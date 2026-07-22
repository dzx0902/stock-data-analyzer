import threading

from gold_agent.bootstrap import initialize
from gold_agent.config import APP_ID, APP_SECRET, settings
from gold_agent.ledger.service import monitor_alerts, monitor_scheduled_reports
from gold_agent.security.backup import monitor_backups
from gold_agent.strategy.service import monitor_strategy_alerts


def main() -> None:
    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise RuntimeError("缺少运行依赖 lark-oapi，请先执行 pip install -e .") from exc

    from gold_agent.integrations.feishu import on_p2_im_message_receive_v1

    initialize()
    if settings.run_local_schedulers:
        threading.Thread(target=monitor_alerts, daemon=True).start()
        threading.Thread(target=monitor_scheduled_reports, daemon=True).start()
        threading.Thread(target=monitor_backups, daemon=True).start()
        threading.Thread(target=monitor_strategy_alerts, daemon=True).start()
    if not settings.run_feishu:
        print("Gold Agent started without Feishu long connection.")
        return
    settings.validate_feishu()
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_p2_im_message_receive_v1)
        .build()
    )
    print("Gold Agent 正在启动飞书长连接...")
    lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    ).start()
