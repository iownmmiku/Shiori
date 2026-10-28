"""Best-effort system tray notifications, with no modal or credential prompt."""
import logging
import os
import threading


def notify_info(message, *, title='Shiori', tray=None, lifetime=8):
    """Return immediately; unavailable desktop notifications never block startup."""
    if os.name != 'nt':
        return False

    def deliver():
        temporary = None
        try:
            if tray is not None:
                tray.notify(message, title)
                return
            import pystray
            from .brand import tray_image
            if not pystray.Icon.HAS_NOTIFICATION:
                raise RuntimeError('system notifications unavailable')
            temporary = pystray.Icon('shiori-notification', tray_image(), title)

            def setup(icon):
                try:
                    icon.visible = True
                    icon.notify(message, title)
                    threading.Event().wait(lifetime)
                except Exception:
                    logging.getLogger(__name__).warning('系统通知发送失败，浏览器模式保持运行。')
                finally:
                    icon.stop()

            temporary.run(setup=setup)
        except Exception:
            logging.getLogger(__name__).warning('系统通知不可用，软件将继续运行；请查看运行日志中的切换记录。')
        finally:
            if temporary is not None:
                try: temporary.stop()
                except Exception: pass

    threading.Thread(target=deliver, name='shiori-notification', daemon=True).start()
    return True
