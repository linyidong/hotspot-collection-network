# -*- coding: utf-8 -*-
"""临时脚本：把崩溃残留的 running 批次标记为 failed。验证后删除。"""
import sqlite3
from datetime import datetime

c = sqlite3.connect(r"data\app.db")
now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
c.execute("UPDATE batches SET status='failed', finished_at=? WHERE status='running'", (now,))
c.commit()
print("残留 running 批次已标记 failed:", c.total_changes, "行")
