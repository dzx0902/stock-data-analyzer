# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from gold_agent.agent.core import run_agent
from gold_agent.config import (
    APP_ID,
    APP_SECRET,
    BASE_URL_QWEN,
    LLM_MODEL_QWEN_VISION,
    MAX_WORKERS,
    QWEN_API_KEY,
    settings,
)
from gold_agent.infra.http import json_loads_safe, safe_float
from gold_agent.ledger.service import (
    add_alert,
    complete_message_processing,
    fail_message_processing,
    format_ledger_summary,
    handle_pending_ledger_confirmation,
    mark_message_processed,
    parse_alert_request,
    parse_ledger_request,
    parse_report_subscription_request,
    save_pending_ledger_entry,
    save_pending_operation,
    update_report_subscription,
)
from gold_agent.market.prices import valid_price
from gold_agent.memory.service import update_user_memory_from_text

def ensure_feishu_config() -> None:
    settings.validate_feishu()


feishu_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .enable_set_token(True)
    .build()
)

message_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="gold-agent")


def reply_message(message_id: str, text: str) -> None:
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    last_err = None
    for i in range(3):
        try:
            feishu_client.im.v1.message.reply(req)
            return
        except Exception as e:
            last_err = e
            print(f"[飞书回复失败] 第{i+1}次: {e}")
            time.sleep(1.5 * (i + 1))
    raise last_err


def send_feishu_message(open_id: str, text: str) -> None:
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    feishu_client.im.v1.message.create(req)


def extract_lark_file_key(upload_response: Any) -> str:
    """Compatible file_key extraction for different lark-oapi versions."""
    # Newer SDKs usually expose response.data.file_key.
    data = getattr(upload_response, "data", None)
    if data:
        file_key = getattr(data, "file_key", None)
        if file_key:
            return file_key
        if isinstance(data, dict):
            file_key = data.get("file_key") or data.get("fileKey")
            if file_key:
                return file_key

    # Some SDK/model versions may put it directly on the response.
    for attr in ["file_key", "fileKey"]:
        file_key = getattr(upload_response, attr, None)
        if file_key:
            return file_key

    # Older code used raw_body; keep it as a fallback only.
    for attr in ["raw_body", "raw", "body"]:
        raw = getattr(upload_response, attr, None)
        if not raw:
            continue
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            file_key = (parsed.get("data") or {}).get("file_key") or parsed.get("file_key")
            if file_key:
                return file_key
        except Exception:
            pass

    # Last chance: inspect __dict__, useful for debugging SDK changes.
    try:
        d = getattr(upload_response, "__dict__", {}) or {}
        for value in d.values():
            if hasattr(value, "file_key") and getattr(value, "file_key"):
                return getattr(value, "file_key")
            if isinstance(value, dict) and value.get("file_key"):
                return value.get("file_key")
    except Exception:
        pass

    return ""


def extract_lark_resource_bytes(response: Any) -> bytes:
    for obj in [response, getattr(response, "data", None)]:
        if obj is None:
            continue
        for attr in ["file", "content", "body", "raw_body"]:
            value = getattr(obj, attr, None)
            if isinstance(value, bytes):
                return value
            if isinstance(value, bytearray):
                return bytes(value)
            if hasattr(value, "read"):
                data = value.read()
                if isinstance(data, bytes):
                    return data
    return b""


def download_feishu_message_image(message_id: str, image_key: str) -> Tuple[bytes, str]:
    """Download an image attached to a received Feishu message."""
    try:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(image_key)
            .type("image")
            .build()
        )
        response = feishu_client.im.v1.message_resource.get(request)
        if hasattr(response, "success") and not response.success():
            return b"", getattr(response, "msg", "飞书图片资源下载失败")
        content = extract_lark_resource_bytes(response)
        if not content:
            return b"", "飞书图片资源响应中没有可读取的图片数据"
        return content, ""
    except Exception as e:
        traceback.print_exc()
        return b"", f"飞书图片下载失败：{e}"


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    parsed = json_loads_safe(text, None)
    if isinstance(parsed, dict):
        return parsed
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        parsed = json_loads_safe(match.group(0), None)
        if isinstance(parsed, dict):
            return parsed
    return {}


def analyze_ledger_screenshot(image_bytes: bytes) -> Dict[str, Any]:
    if not image_bytes:
        return {"status": "error", "message": "图片内容为空"}
    if len(image_bytes) > 15 * 1024 * 1024:
        return {"status": "error", "message": "图片超过 15MB，请压缩后重新发送"}
    if not QWEN_API_KEY:
        return {"status": "error", "message": "未配置 QWEN_API_KEY，无法识别截图"}

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
        mime_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        mime_type = "image/jpeg"
    data_url = f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode("ascii")
    prompt = """
识别这张图片中所有已经发生的黄金交易。它可能是购买小票、订单列表、订单详情、银行积存金成交页、
黄金回收结算单或聊天截图。逐行检查图片，只提取明确可见的信息，不要猜测，也不要只返回第一笔。

返回一个 JSON 对象，不要输出 Markdown：
{
  "is_gold_transaction": true,
  "transactions": [
    {
      "side": "buy 或 sell 或 unknown",
      "quantity_grams": 数字或 null,
      "unit_price_cny": 数字或 null,
      "total_amount_cny": 数字或 null,
      "fee_cny": 数字或 null,
      "merchant": "商家/银行/渠道或空字符串",
      "trade_date": "日期或空字符串",
      "product": "黄金品种或空字符串",
      "confidence": 0到1,
      "evidence": ["支持这些字段的简短原文"],
      "missing_fields": ["缺失或不确定的关键字段"],
      "warning": "歧义说明或空字符串"
    }
  ]
}

规则：
1. 购买、付款、成交买入、申购记为 buy；回收、卖出、结算到账记为 sell。
2. 公斤换算为克。金额统一为人民币元；无法确认币种时不要填人民币金额。
3. 每克价格和总金额都存在时保留两者。只出现总金额和克重也可以。
4. “工费已包含在实付金额”时不要把它重复加到 fee_cny；只有明确独立收费才填写 fee_cny。
5. 图片不是黄金交易凭证，is_gold_transaction=false。
6. 图片中每个独立订单、成交行或结算记录分别放入 transactions；不得合并多笔交易。
7. 即使只有一笔交易，也必须放入 transactions 数组。
"""
    try:
        client = OpenAI(api_key=QWEN_API_KEY, base_url=BASE_URL_QWEN)
        response = client.chat.completions.create(
            model=LLM_MODEL_QWEN_VISION,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=0,
        )
        payload = _extract_json_object(response.choices[0].message.content or "")
        if not payload:
            return {"status": "error", "message": "视觉模型没有返回可解析的结构化结果"}
        payload["status"] = "success"
        return payload
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "message": f"截图识别失败：{e}"}


def _build_single_screenshot_ledger_draft(result: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    if result.get("status") != "success":
        return None, result.get("message", "截图识别失败")
    if not result.get("is_gold_transaction"):
        return None, "这张图片没有识别到明确的黄金交易记录。"

    side = str(result.get("side", "")).strip().lower()
    quantity = safe_float(result.get("quantity_grams"))
    unit_price = safe_float(result.get("unit_price_cny"))
    total_amount = safe_float(result.get("total_amount_cny"))
    fee = safe_float(result.get("fee_cny"))
    fee = 0.0 if fee is None else fee
    confidence = safe_float(result.get("confidence")) or 0.0
    missing = list(result.get("missing_fields") or [])

    if side not in ["buy", "sell"]:
        missing.append("买入或卖出方向")
    if quantity is None or quantity <= 0:
        missing.append("黄金克重")
    if unit_price is None and total_amount is None:
        missing.append("每克价格或总金额")
    if missing or confidence < 0.70:
        detail = "、".join(dict.fromkeys(str(x) for x in missing if x)) or "识别置信度不足"
        evidence = "；".join(str(x) for x in (result.get("evidence") or [])[:5])
        return None, f"截图已识别，但还不能安全入账：{detail}。" + (f"\n识别依据：{evidence}" if evidence else "")

    if unit_price is None and total_amount is not None and quantity:
        unit_price = total_amount / quantity
    if unit_price is None or not valid_price(unit_price, "CNY/g"):
        return None, f"截图中的成交单价 {unit_price} 元/克超出合理范围，请人工确认。"

    note_parts = [
        str(result.get("merchant", "") or "").strip(),
        str(result.get("product", "") or "").strip(),
        str(result.get("trade_date", "") or "").strip(),
        "截图识别",
    ]
    draft = {
        "side": side,
        "quantity_grams": round(quantity, 4),
        "unit_price_cny": round(unit_price, 4),
        "total_amount_cny": round(total_amount, 2) if total_amount is not None else None,
        "fee_cny": round(fee, 2),
        "note": "；".join(x for x in note_parts if x),
        "confidence": round(confidence, 3),
    }
    side_text = "买入" if side == "buy" else "卖出"
    lines = [
        "已从截图识别出一笔待确认交易：",
        f"- 方向：{side_text}",
        f"- 克重：{quantity:.4f} 克",
        f"- 成交单价：{unit_price:.2f} 元/克",
    ]
    if total_amount is not None:
        lines.append(f"- 总金额：{total_amount:.2f} 元")
    if fee:
        lines.append(f"- 独立费用：{fee:.2f} 元")
    if draft["note"]:
        lines.append(f"- 备注：{draft['note']}")
    lines.append(f"- 识别置信度：{confidence:.0%}")
    lines.append("")
    lines.append("请回复“确认记账”写入账本，或回复“取消记账”。")
    return draft, "\n".join(lines)


def build_screenshot_ledger_draft(result: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    if result.get("status") != "success":
        return None, result.get("message", "截图识别失败")
    if not result.get("is_gold_transaction"):
        return None, "这张图片没有识别到明确的黄金交易记录。"

    transactions = result.get("transactions")
    if not isinstance(transactions, list):
        # Backward compatibility for vision models returning the old single-record schema.
        transactions = [result]

    drafts: List[Dict[str, Any]] = []
    rejected: List[str] = []
    for index, transaction in enumerate(transactions, start=1):
        if not isinstance(transaction, dict):
            rejected.append(f"第 {index} 笔结构无效")
            continue
        item = dict(transaction)
        item["status"] = "success"
        item["is_gold_transaction"] = True
        draft, message = _build_single_screenshot_ledger_draft(item)
        if draft:
            drafts.append(draft)
        else:
            rejected.append(f"第 {index} 笔：{message}")

    if not drafts:
        detail = "\n".join(rejected[:5])
        return None, "截图中没有可安全入账的完整交易。" + (f"\n{detail}" if detail else "")

    lines = [f"已从截图识别出 {len(drafts)} 笔待确认交易："]
    for index, draft in enumerate(drafts, start=1):
        side_text = "买入" if draft["side"] == "buy" else "卖出"
        lines.append(
            f"{index}. {side_text} {draft['quantity_grams']:.4f} 克，"
            f"{draft['unit_price_cny']:.2f} 元/克"
        )
    if rejected:
        lines.append(f"另有 {len(rejected)} 笔因字段缺失或置信度不足未加入草稿。")
    lines.extend(["", "请回复“确认记账”批量写入账本，或回复“取消记账”。"])
    return {"transactions": drafts}, "\n".join(lines)


def upload_file_to_feishu(chat_id: str, file_path: str) -> Tuple[bool, str]:
    """上传 Word 文件到飞书并发送。

    关键修复：
    - 飞书文件类型不要写 docx，使用 doc（飞书侧文档类）更容易被手机端正确识别；
    - SDK 的 file 参数传文件对象，不传 bytes，避免部分版本把 bytes repr 当成文本流处理，手机端出现 b'PK...' 乱码；
    - 如果 doc 上传失败，自动用 stream 再试一次。
    """
    if not os.path.exists(file_path):
        msg = f"文件不存在：{file_path}"
        print(f"[飞书文件上传] {msg}")
        return False, msg

    def _upload_once(file_type: str):
        file_name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            upload_request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(file_name)
                    .file(f)
                    .build()
                )
                .build()
            )
            return feishu_client.im.v1.file.create(upload_request)

    try:
        upload_response = None
        last_msg = ""
        for file_type in ["doc", "stream"]:
            upload_response = _upload_once(file_type)
            if hasattr(upload_response, "success") and upload_response.success():
                print(f"[飞书文件上传] file_type={file_type} 上传成功")
                break
            last_msg = getattr(upload_response, "msg", "未知错误") if upload_response else "无响应"
            print(f"[飞书文件上传失败] file_type={file_type} msg={last_msg}")
        else:
            return False, last_msg or "上传失败"

        file_key = extract_lark_file_key(upload_response)
        if not file_key:
            debug_attrs = sorted([a for a in dir(upload_response) if not a.startswith("_")])[:100]
            msg = "上传成功但未能从响应中解析 file_key；response attrs=" + repr(debug_attrs)
            print("[飞书文件上传失败] " + msg)
            return False, msg

        send_file_req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        send_resp = feishu_client.im.v1.message.create(send_file_req)
        if hasattr(send_resp, "success") and not send_resp.success():
            msg = getattr(send_resp, "msg", "发送文件消息失败")
            print(f"[飞书文件发送失败] {msg}")
            return False, msg

        try:
            os.remove(file_path)
        except Exception:
            pass
        return True, "已上传"
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        print(f"[飞书文件上传异常] {msg}")
        return False, msg

def async_worker_task(message_id: str, chat_id: str, user_text: str, sender_id: str) -> None:
    try:
        confirmation_answer = handle_pending_ledger_confirmation(sender_id, user_text)
        if confirmation_answer is not None:
            update_user_memory_from_text(sender_id, user_text, confirmation_answer)
            reply_message(message_id, confirmation_answer)
            complete_message_processing(message_id)
            return

        ledger_request = parse_ledger_request(user_text)
        if ledger_request:
            action = ledger_request["action"]
            if action == "add":
                save_pending_operation(
                    sender_id,
                    "ledger.add",
                    {
                        "side": ledger_request["side"],
                        "quantity_grams": ledger_request["quantity_grams"],
                        "unit_price_cny": ledger_request["unit_price_cny"],
                        "fee_cny": ledger_request["fee_cny"],
                        "note": ledger_request["note"],
                    },
                    message_id,
                )
                answer = (
                    "已生成待确认记账草稿。"
                    f"\n方向：{ledger_request['side']}"
                    f"\n克重：{ledger_request['quantity_grams']}"
                    f"\n单价：{ledger_request['unit_price_cny']} 元/克"
                    "\n请回复“确认记账”执行，或回复“取消记账”。"
                )
            elif action == "undo":
                save_pending_operation(sender_id, "ledger.undo", {}, message_id)
                answer = "已生成撤销最近一笔交易的待确认操作。请回复“确认操作”执行，或回复“取消操作”。"
            elif action == "summary":
                answer = format_ledger_summary(sender_id)
            else:
                answer = ledger_request.get("message", "记账指令不完整。")
            update_user_memory_from_text(sender_id, user_text, answer)
            reply_message(message_id, answer)
            complete_message_processing(message_id)
            return

        subscription_request = parse_report_subscription_request(user_text)
        if subscription_request:
            answer = update_report_subscription(sender_id, subscription_request)
            update_user_memory_from_text(sender_id, user_text, answer)
            reply_message(message_id, answer)
            complete_message_processing(message_id)
            return

        alert = parse_alert_request(user_text)
        if alert:
            add_alert(
                user_id=sender_id,
                condition_type=alert["condition_type"],
                target_price=alert["target_price"],
                gold_type=alert["gold_type"],
            )
            unit = "美元/盎司" if alert["gold_type"] == "international" else "元/克"
            cond_zh = "低于" if alert["condition_type"] == "below" else "高于"
            answer = f"黄金价格监控已挂载。\n条件：金价{cond_zh} {alert['target_price']} {unit}\n触发后我会通过飞书提醒你。"
            update_user_memory_from_text(sender_id, user_text, answer)
            reply_message(message_id, answer)
            complete_message_processing(message_id)
            return

        answer = run_agent(user_text, sender_id)
        update_user_memory_from_text(sender_id, user_text, answer)
        reply_message(message_id, answer)

        current_thread = threading.current_thread()
        file_path = getattr(current_thread, "uploaded_file_path", None)
        if file_path:
            ok, msg = upload_file_to_feishu(chat_id, file_path)
            if not ok:
                reply_message(message_id, "Word 报告已生成，但飞书文件上传失败：" + msg + "\n文件路径：" + file_path)
    except Exception as e:
        traceback.print_exc()
        reply_message(message_id, f"系统控制链路抛错：{e}")


    else:
        complete_message_processing(message_id)


def async_image_worker_task(message_id: str, chat_id: str, image_key: str, sender_id: str) -> None:
    try:
        image_bytes, error = download_feishu_message_image(message_id, image_key)
        if not image_bytes:
            reply_message(message_id, error or "未能下载这张图片。")
            return
        result = analyze_ledger_screenshot(image_bytes)
        draft, answer = build_screenshot_ledger_draft(result)
        if draft:
            for transaction in draft.get("transactions", []):
                transaction["image_refs"] = [image_key]
            save_pending_ledger_entry(sender_id, message_id, draft)
        update_user_memory_from_text(sender_id, "[用户发送黄金交易截图]", answer)
        reply_message(message_id, answer)
    except Exception as e:
        traceback.print_exc()
        reply_message(message_id, f"截图记账处理失败：{e}")


    else:
        complete_message_processing(message_id)


def on_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        message_id = msg.message_id
        chat_id = msg.chat_id

        if not mark_message_processed(message_id):
            return

        sender = data.event.sender.sender_id
        sender_id = getattr(sender, "open_id", None) or getattr(sender, "user_id", None)
        if not sender_id:
            return

        message_type = str(getattr(msg, "message_type", "") or getattr(msg, "msg_type", "")).lower()
        try:
            content_obj = json.loads(msg.content)
        except Exception:
            content_obj = {}

        if message_type == "image" or (isinstance(content_obj, dict) and content_obj.get("image_key")):
            image_key = str(content_obj.get("image_key", "")).strip()
            if not image_key:
                reply_message(message_id, "图片消息中没有读取到 image_key。")
                return
            print(f"\n[飞书图片事件接入] message_id={message_id} image_key={image_key}")
            message_executor.submit(async_image_worker_task, message_id, chat_id, image_key, sender_id)
            return

        user_text = str(content_obj.get("text", "") if isinstance(content_obj, dict) else "").strip()
        if not user_text:
            user_text = str(msg.content).strip()
        if not user_text:
            return

        print(f"\n[飞书事件接入] message_id={message_id} user_text={user_text}")
        message_executor.submit(async_worker_task, message_id, chat_id, user_text, sender_id)
    except Exception as e:
        traceback.print_exc()
        print(f"[飞书接收端异常] {e}")
