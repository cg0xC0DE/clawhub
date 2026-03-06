"""查询消息池最新消息（增量模式）"""
import json, urllib.request, sys
from pathlib import Path

HUB = "http://127.0.0.1:61000"
AGENT_ID = "__AGENT_ID__"
CURSOR_FILE = Path(__file__).parent / ".eunuch_cursor"

since = CURSOR_FILE.read_text(encoding="utf-8").strip() if CURSOR_FILE.exists() else None

url = f"{HUB}/api/eunuch/query?agent={AGENT_ID}&limit=50"
if since:
    url += f"&since={since}"

try:
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
except Exception as e:
    print(f"查阅内参失败: {e}", file=sys.stderr)
    sys.exit(1)

if data.get("message_count", 0) > 0:
    latest = data.get("latest_timestamp")
    if latest:
        CURSOR_FILE.write_text(latest, encoding="utf-8")
    print(data.get("text", "（无内参）"))
else:
    print("（暂无新消息）")
