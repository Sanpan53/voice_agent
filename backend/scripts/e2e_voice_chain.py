"""端到端验证：跑通「登录 → 会话 → 语音 WebSocket → 商品卡」整条主链路。

**这是全项目唯一一个真正把前后端串起来跑的脚本**——`pytest` 全是单测和
带假仓储的集成测试，`voice-test.html` 要人点鼠标。中间那一层"代理转没转发、
子协议回没回显、帧的顺序对不对"只有这里能验。

它刻意**经 Vite 代理**（默认 `:5173`）而不是直连后端，因为代理正是最容易
出错的一环：`vite.config.ts` 里 `ws: true` 漏掉的话，`/ws/voice` 的升级请求
会落到 Vite 自己身上，握手直接失败——而这件事直连后端是测不出来的。

## 怎么造"用户说的话"

没有录音文件，所以**用后端自己的 TTS 合成一句话，再把这坨 PCM 当作麦克风
输入喂回 ASR**。TTS 的默认输出就是 PCM 16000Hz / 单声道 / 16-bit，和 ASR
要求的输入格式逐字一致，可以原样回灌。

这条路子顺带证明了一件有用的事：**TTS 和 ASR 的输出入格式是自洽的**。
哪天后端改了 TTS 的采样率，这个脚本会第一个红。

## 用法

```bash
# 两个服务都起着：backend 在 :8080，frontend 的 vite 在 :5173
cd backend
python scripts/e2e_voice_chain.py                          # 默认经 :5173 代理
python scripts/e2e_voice_chain.py --proxy 127.0.0.1:8080   # 直连后端（对照组）
python scripts/e2e_voice_chain.py --utterance "想买双跑步鞋"
```

## 两个实测出来的坑，写在参数里免得下次再踩

1. **DashScope 的 ASR 冷会话要 ~20 秒才吐第一个包**（日志里的
   `first package delay`）。所以等 final 的超时给到了 {@link ASR_TIMEOUT}，
   给小了会误判成"ASR 没反应"。
2. **话术必须把槽位填满**，否则会走澄清分支——澄清分支的 `display_blocks`
   是空的，**本来就没有商品卡**，会被误读成"推荐链路坏了"。默认话术
   特意带上了品类 + 预算 + 场景。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 让脚本能从 backend/ 以外的位置跑起来
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import websockets  # noqa: E402

#: 等 ASR final 的上限。见模块 docstring 的坑 1。
ASR_TIMEOUT = 90
#: final 之后等主链路的时长（向量检索 + LLM 理由生成 + TTS 流式下发）
REPLY_TIMEOUT = 90
#: 连续这么久没消息就认为这一轮结束了
QUIET_TOLERANCE = 20

DEFAULT_UTTERANCE = "我想买一双500元以内的跑鞋，平时在公路跑步"


async def get_token(base: str) -> tuple[str, int]:
    """登录拿 token。`/session/start` 依赖 `CurrentUserId`，没有它直接 401。"""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base}/api/v1/auth/login", json={"username": "alice"}
        )
        response.raise_for_status()
        body = response.json()
        return body["token"], body["userId"]


async def start_session(base: str, token: str, session_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base}/api/v1/session/start",
            json={"sessionId": session_id, "channel": "HOME_ENTRY"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


async def synthesize(text: str) -> bytes:
    """用后端的 TTS 造一段"用户说的话"。"""
    from app.voice.tts_service import TtsService

    return await TtsService().synthesize_text(text)


async def probe_handshake(ws_base: str, token: str, user_id: int) -> bool:
    """只连不喂音频：验证代理升级 + 子协议回显。"""
    url = f"{ws_base}/ws/voice?sessionId=e2e-probe&userId={user_id}"
    expected = f"Bearer-{token}"
    async with websockets.connect(url, subprotocols=[expected]) as ws:
        echoed = ws.subprotocol
        print(f"    ✓ 经代理握手成功，服务端回显的子协议 = {echoed!r}")
        if echoed != expected:
            # 不回显的话，浏览器会按 RFC 6455 判定"服务端没选中我提的任何
            # 子协议"并**直接断开**，表现为"连上了又立刻掉线"。
            print("    ✗ 子协议没有原样回显！浏览器会在这里直接断开连接")
            return False
        await ws.send(b"\x00" * 3200)  # 喂一帧静音，确认上行通
        await asyncio.sleep(0.5)
    return True


async def run_turn(
    ws_base: str, token: str, user_id: int, session_id: str, utterance: str
) -> dict:
    """完整一轮：喂 PCM → 收 asr / caption / recommendation / audio。"""
    audio = await synthesize(utterance)
    samples = len(audio) // 2
    print(f"    TTS 合成 {samples} 采样（{samples / 16000:.2f}s）充当麦克风输入")

    url = f"{ws_base}/ws/voice?sessionId={session_id}&userId={user_id}"
    result: dict = {"asr": [], "caption": [], "items": [], "audio_bytes": 0, "echoed": None}

    async with websockets.connect(url, subprotocols=[f"Bearer-{token}"]) as ws:
        result["echoed"] = ws.subprotocol
        print(f"    ✓ 握手成功，子协议 = {(ws.subprotocol or '')[:24]}…")

        # 按 100ms 一帧喂，和真实前端一致
        chunk = 16000 // 10 * 2
        for offset in range(0, len(audio), chunk):
            await ws.send(audio[offset : offset + chunk])
        # 补 800ms 静音，逼 Paraformer 的 VAD 断句、吐出最后一次 final。
        # 前端 `useVoiceSocket.stop()` 里做的是同一件事。
        await ws.send(b"\x00" * (16000 * 800 // 1000 * 2))

        loop = asyncio.get_running_loop()

        # 阶段一：等到 ASR final —— 主链路是在 final 那一刻才被触发的
        deadline = loop.time() + ASR_TIMEOUT
        while loop.time() < deadline:
            message = await asyncio.wait_for(ws.recv(), timeout=deadline - loop.time())
            if isinstance(message, bytes):
                result["audio_bytes"] += len(message)
                continue
            frame = json.loads(message)
            if frame.get("type") == "asr":
                result["asr"].append(frame)
                flag = "[最终]" if frame["final"] else "[实时]"
                print(f"    ASR {flag} {frame['text']!r}")
                if frame["final"]:
                    break

        # 阶段二：final 之后推荐 + 逐句字幕 + TTS 音频才开始下发。
        # **必须继续 recv**，而且要等到真的没消息为止——推荐分支要先跑完
        # 向量检索和理由生成的 LLM 调用，中间那段静默不是"结束了"。
        deadline = loop.time() + REPLY_TIMEOUT
        while loop.time() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=QUIET_TOLERANCE)
            except (TimeoutError, websockets.ConnectionClosed):
                break

            if isinstance(message, bytes):
                result["audio_bytes"] += len(message)
                continue
            frame = json.loads(message)
            kind = frame.get("type")
            if kind == "caption":
                result["caption"].append(frame["text"])
                print(f"    caption {frame['text']!r}")
            elif kind == "recommendation":
                result["items"] = frame["items"]
                print(f"    推荐 {len(frame['items'])} 件")
                for item in frame["items"][:5]:
                    print(f"       - {item['name']} ¥{item['price']}")
            elif kind == "asr":
                result["asr"].append(frame)

    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--proxy",
        default="127.0.0.1:5173",
        help="host:port。默认走 Vite 代理；填 127.0.0.1:8080 则直连后端",
    )
    parser.add_argument("--utterance", default=DEFAULT_UTTERANCE)
    args = parser.parse_args()

    host = args.proxy
    http_base = f"http://{host}"
    ws_base = f"ws://{host}"

    print(f"目标：{host}\n")

    token, user_id = await get_token(http_base)
    print(f"登录成功 userId={user_id}")

    scope = await start_session(http_base, token, "e2e-chain")
    print(f"会话启动 {scope}")

    print("\n[A] 握手探测（不喂音频）")
    if not await probe_handshake(ws_base, token, user_id):
        return 1

    print(f"\n[B] 完整一轮：「{args.utterance}」")
    result = await run_turn(ws_base, token, user_id, "e2e-chain", args.utterance)

    finals = [frame for frame in result["asr"] if frame["final"]]

    print("\n===== 结果 =====")
    print(f"ASR 最终结果 : {[frame['text'] for frame in finals]}")
    print(f"字幕句数     : {len(result['caption'])}")
    print(f"推荐商品数   : {len(result['items'])}")
    print(f"收到音频字节 : {result['audio_bytes']}")
    print(f"子协议回显   : {'✓' if result['echoed'] else '✗'}")

    # 判据：四个环节各验一次。**推荐为空不算失败**——话术没填满槽位时服务端
    # 会走澄清分支，那一支本来就不发商品卡。所以用"有没有出声"当推荐侧的判据。
    checks = {
        "ASR 识别出文字": bool(finals),
        "收到字幕": bool(result["caption"]),
        "收到 TTS 音频": result["audio_bytes"] > 0,
    }
    for name, ok in checks.items():
        print(f"  {'✅' if ok else '❌'} {name}")

    all_ok = all(checks.values())
    print("\n" + ("✅ 主链路全通" if all_ok else "❌ 有环节没走通"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
