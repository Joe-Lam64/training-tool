"""
外訓課程自動整合工具 v4.0
================================
v4 更新內容:
  1. 介面大改版 (淡藍綠柔和配色)
  2. 協會勾選改為大型卡片
  3. 取消按鈕加大加明顯
  4. 表頭固定 (sticky)
  5. 使用時間計算器 (右上角)
  6. 信件表格邊框加粗
  7. 顯示在線人數 (取代單人限制)
  8. 預設「複訓」
  9. 移除重複的「不要外籍」選項
  10. 加入課程地點+上課時間欄位
  11. 顯示報名網址 (給後台看)
  12. 兩種信件: 同仁版 (簡潔) + 後台版 (完整)
  + 定時更新設定面板 (預設關閉)
"""

import os
import json
import re
import sqlite3
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, render_template_string, session, redirect, url_for
import requests
from bs4 import BeautifulSoup

APP_DIR = Path(__file__).parent
DATA_FILE = APP_DIR / "course_data.json"
DB_FILE = APP_DIR / "users.db"
PORT = 5000

DEFAULT_USERS = [
    {"username": "train0", "password": "HR123", "role": "admin", "display_name": "管理員"},
    {"username": "train1", "password": "1234",  "role": "user",  "display_name": "使用者1"},
    {"username": "train2", "password": "5678",  "role": "user",  "display_name": "使用者2"},
    {"username": "train3", "password": "6789",  "role": "user",  "display_name": "使用者3"},
]

# 分會地址資料庫 (官方據點地址)
BRANCH_ADDRESSES = {
    "中壢": "桃園市中壢區中央西路二段30號10樓/14樓/15樓",
    "桃園": "桃園市桃園區建國路99號4樓",
    "新竹": "新竹市東區光復路二段101號 (詳細地址依課程而定)",
}

# 在線使用者追蹤 (key 用 "username|IP" 區分,這樣同帳號不同電腦也能正確計算)
ONLINE_USERS = {}  # {"username|ip": last_active_timestamp}
ONLINE_LOCK = threading.Lock()


def _make_key(username, ip):
    return f"{username}|{ip}"


def update_online(username, ip="?"):
    with ONLINE_LOCK:
        ONLINE_USERS[_make_key(username, ip)] = time.time()


def get_online_users():
    """取得 5 分鐘內活躍的使用者 (key 列表)"""
    with ONLINE_LOCK:
        cutoff = time.time() - 300
        active = {k: t for k, t in ONLINE_USERS.items() if t > cutoff}
        for k in list(ONLINE_USERS.keys()):
            if ONLINE_USERS[k] <= cutoff:
                del ONLINE_USERS[k]
        return list(active.keys())


def remove_online(username, ip="?"):
    with ONLINE_LOCK:
        k = _make_key(username, ip)
        if k in ONLINE_USERS:
            del ONLINE_USERS[k]


# ==========================================================================
# 資料庫
# ==========================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            display_name TEXT,
            created_at TEXT
        )
    """)
    for u in DEFAULT_USERS:
        cur.execute("SELECT id FROM users WHERE username=?", (u["username"],))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password, role, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (u["username"], u["password"], u["role"], u["display_name"], datetime.now().isoformat())
            )
    conn.commit()
    conn.close()


def verify_user(username, password):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ==========================================================================
# 課程大綱資料庫 (25 種證照)
# ==========================================================================
COURSE_TEMPLATES = {
    "粉塵作業主管": {
        "law": "依職業安全衛生法第23條及職業安全衛生教育訓練規則第11條規定 (初訓18小時)\n依職業安全衛生教育訓練規則第18條規定 (複訓每3年至少3小時)",
        "purpose": "雇主對粉塵作業主管,應使其接受有害作業主管安全教育訓練,以減少職場傷害,達到零工安事故的目標。",
        "outline_initial": ["1. 粉塵作業安全衛生相關法規 (3小時)", "2. 粉塵危害預防標準 (3小時)", "3. 粉塵危害及測定 (3小時)", "4. 粉塵作業環境改善及安全衛生防護具 (3小時)", "5. 通風換氣裝置及其維護 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 粉塵作業場所之危害評估及預防", "2. 粉塵作業相關法規及行政管理與執行", "3. 經驗及問題分享"],
    },
    "有機溶劑": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對有機溶劑作業主管,應使其接受有害作業主管安全教育訓練,以預防有機溶劑中毒及其相關危害。",
        "outline_initial": ["1. 有機溶劑作業相關法規 (3小時)", "2. 有機溶劑中毒預防規則 (3小時)", "3. 有機溶劑之危害及測定 (3小時)", "4. 有機溶劑作業環境改善及安全衛生防護具 (3小時)", "5. 通風換氣裝置及其維護 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 有機溶劑作業場所之危害評估及預防", "2. 有機溶劑作業相關法規及行政管理與執行", "3. 經驗及問題分享"],
    },
    "特定化學": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對特定化學物質作業主管,應使其接受有害作業主管安全教育訓練,以預防特定化學物質之危害。",
        "outline_initial": ["1. 特定化學物質作業相關法規 (3小時)", "2. 特定化學物質危害預防標準 (3小時)", "3. 特定化學物質之危害及測定 (3小時)", "4. 特定化學物質作業環境改善 (3小時)", "5. 通風換氣裝置 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 特定化學物質作業場所之危害評估及預防", "2. 相關法規及管理執行", "3. 經驗及問題分享"],
    },
    "缺氧": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對缺氧作業主管,應使其接受有害作業主管安全教育訓練,以預防缺氧災害及保護勞工安全。",
        "outline_initial": ["1. 缺氧事故處理及急救 (3小時)", "2. 缺氧危險場所之環境測定 (3小時)", "3. 缺氧危險場所危害預防及防護具 (3小時)", "4. 缺氧危險作業安全衛生管理 (3小時)", "5. 缺氧作業相關法規 (3小時)", "6. 缺氧症預防規則 (3小時)"],
        "outline_retraining": ["1. 缺氧危險作業場所之危害評估及預防", "2. 缺氧作業相關法規及管理執行", "3. 經驗及問題分享"],
    },
    "鉛作業": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對鉛作業主管,應使其接受有害作業主管安全教育訓練,以預防鉛中毒及相關危害。",
        "outline_initial": ["1. 鉛作業相關法規 (3小時)", "2. 鉛中毒預防規則 (3小時)", "3. 鉛之危害及測定 (3小時)", "4. 鉛作業環境改善及防護具 (3小時)", "5. 通風換氣裝置 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 鉛作業場所之危害評估及預防", "2. 鉛作業相關法規及管理執行", "3. 經驗及問題分享"],
    },
    "施工架": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對施工架組配作業主管,應使其接受營造作業主管安全教育訓練,以預防墜落及倒塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 施工架組配作業危害預防 (3小時)", "3. 施工架構造、組配與拆除作業安全 (3小時)", "4. 防止墜落、感電及倒塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 施工架組配作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "屋頂作業": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對屋頂作業主管,應使其接受營造作業主管安全教育訓練,以預防墜落災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 屋頂作業危害預防 (3小時)", "3. 屋頂作業安全要領 (3小時)", "4. 墜落災害防止計畫 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 屋頂作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "擋土支撐": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對擋土支撐作業主管,應使其接受營造作業主管安全教育訓練,以預防崩塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 擋土支撐作業危害預防 (3小時)", "3. 擋土支撐構造、組立與拆除 (3小時)", "4. 土壤性質與穩定分析 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 擋土支撐作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "模板支撐": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對模板支撐作業主管,應使其接受營造作業主管安全教育訓練,以預防倒塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 模板支撐作業危害預防 (3小時)", "3. 模板支撐構造、組立與拆除 (3小時)", "4. 防止倒塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 模板支撐作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "鋼構組配": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對鋼構組配作業主管,應使其接受營造作業主管安全教育訓練,以預防墜落及倒塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 鋼構組配作業危害預防 (3小時)", "3. 鋼構構造、組配與拆除作業 (3小時)", "4. 防止墜落、感電及倒塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 鋼構組配作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "露天開挖": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對露天開挖作業主管,應使其接受營造作業主管安全教育訓練,以預防崩塌及墜落災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 露天開挖作業危害預防 (3小時)", "3. 土壤性質與穩定分析 (3小時)", "4. 防止崩塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 露天開挖作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "堆高機": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對荷重在一公噸以上之堆高機操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 堆高機相關法規 (3小時)", "2. 堆高機之構造、性能與安全裝置 (3小時)", "3. 堆高機操作技術與實作 (6小時)", "4. 堆高機自動檢查及維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新堆高機相關法規修訂", "2. 堆高機自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "高空工作車": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓24小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對高空工作車操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 高空工作車相關法規 (3小時)", "2. 高空工作車之構造、性能與安全裝置 (3小時)", "3. 高空工作車操作技術與實作 (12小時)", "4. 高空工作車自動檢查及維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新高空工作車相關法規修訂", "2. 高空工作車自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "固定式起重機": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18-38小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對固定式起重機操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 固定式起重機相關法規 (3小時)", "2. 起重機之構造、性能與安全裝置 (3小時)", "3. 起重機操作技術與實作 (10-25小時)", "4. 自動檢查與維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新固定式起重機相關法規修訂", "2. 起重機具自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "移動式起重機": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18-38小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對移動式起重機操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 移動式起重機相關法規 (3小時)", "2. 起重機之構造、性能與安全裝置 (3小時)", "3. 起重機操作技術與實作 (10-25小時)", "4. 自動檢查與維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新移動式起重機相關法規修訂", "2. 起重機具自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "吊掛": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對使用起重機具從事吊掛作業人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 吊掛作業相關法規 (3小時)", "2. 吊掛器具之種類、構造與使用 (3小時)", "3. 吊掛作業技術與實作 (6小時)", "4. 吊掛器具之檢查與維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 吊掛作業相關法規", "2. 吊掛器具之檢查與維護", "3. 吊掛作業安全要領與事故預防"],
    },
    "鍋爐": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓50小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對鍋爐操作人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 鍋爐相關法規 (3小時)", "2. 鍋爐之構造、原理與安全裝置 (6小時)", "3. 鍋爐操作技術與實作 (30小時)", "4. 鍋爐自動檢查與維護 (6小時)", "5. 異常處置與災害預防 (5小時)"],
        "outline_retraining": ["1. 鍋爐相關法規", "2. 鍋爐自動檢查與維護", "3. 異常處置與事故預防"],
    },
    "壓力容器": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓35小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對第一種壓力容器操作人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 壓力容器相關法規 (3小時)", "2. 壓力容器之構造、原理與安全裝置 (6小時)", "3. 壓力容器操作技術與實作 (18小時)", "4. 壓力容器自動檢查與維護 (3小時)", "5. 異常處置與災害預防 (5小時)"],
        "outline_retraining": ["1. 壓力容器相關法規", "2. 壓力容器自動檢查", "3. 異常處置與事故預防"],
    },
    "高壓氣體": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓21-35小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對高壓氣體操作人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 高壓氣體相關法規 (3小時)", "2. 高壓氣體之性質與危害 (3小時)", "3. 高壓氣體設備之構造與操作 (12小時)", "4. 自動檢查與維護 (3小時)", "5. 異常處置與緊急應變 (3小時)"],
        "outline_retraining": ["1. 高壓氣體相關法規", "2. 高壓氣體設備之檢查與維護", "3. 異常處置與緊急應變"],
    },
    "急救人員": {
        "law": "依職業安全衛生教育訓練規則第15條 (初訓16小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主應使急救人員接受急救人員安全衛生教育訓練,以具備緊急傷病處置之知識及技能。",
        "outline_initial": ["1. 急救相關法規與基本知識 (2小時)", "2. 心肺復甦術 (CPR) 與 AED 操作 (4小時)", "3. 創傷及出血處理 (2小時)", "4. 燒燙傷、化學灼傷處理 (2小時)", "5. 中毒、休克、骨折處理 (2小時)", "6. 緊急應變與運送 (2小時)", "7. 案例演練 (2小時)"],
        "outline_retraining": ["1. 急救相關法規更新", "2. CPR 與 AED 操作複習", "3. 常見傷病處置與案例演練"],
    },
    "職業安全衛生業務主管": {
        "law": "依職業安全衛生教育訓練規則第3條 (初訓21-42小時) / 第18條 (複訓每2年6小時)",
        "purpose": "雇主應使各事業單位之職業安全衛生業務主管接受教育訓練,以具備辦理職業安全衛生管理事項之能力。",
        "outline_initial": ["1. 職業安全衛生相關法規 (6小時)", "2. 職業安全衛生政策與管理計畫 (3小時)", "3. 危害辨識、風險評估與管理 (6小時)", "4. 職業災害調查、分析與案例研討 (3小時)", "5. 安全衛生管理實務 (3小時)"],
        "outline_retraining": ["1. 最新職安衛法規修訂", "2. 安全衛生管理實務新知", "3. 危害辨識與風險評估", "4. 案例分析與經驗分享"],
    },
    "職業安全衛生管理": {
        "law": "依職業安全衛生教育訓練規則第7條 (初訓115小時) / 第18條 (複訓每2年12小時)",
        "purpose": "雇主應使職業安全衛生管理人員接受教育訓練。",
        "outline_initial": ["1. 職業安全衛生法令、政策與管理系統 (12小時)", "2. 安全衛生概論與風險評估 (24小時)", "3. 機械、電氣、化學、火災爆炸危害預防 (24小時)", "4. 物理性、人因工程、健康管理 (24小時)", "5. 職業災害調查、分析與緊急應變 (12小時)", "6. 安全衛生管理實務、稽核與績效 (19小時)"],
        "outline_retraining": ["1. 最新職安衛法規修訂", "2. 風險評估與管理新趨勢", "3. 安全衛生績效管理", "4. 案例研討與經驗分享"],
    },
    "防火管理": {
        "law": "依消防法第13條及消防法施行細則第14條 (初訓12小時) / 複訓每3年6小時",
        "purpose": "依消防法規定,管理權人應指定防火管理人,並使其接受訓練,以製定消防防護計畫並執行火災預防工作。",
        "outline_initial": ["1. 消防法令與防火管理制度 (3小時)", "2. 火災基本知識與成因分析 (3小時)", "3. 消防安全設備之認識與維護 (3小時)", "4. 消防防護計畫之製定與推行 (3小時)"],
        "outline_retraining": ["1. 消防法規修訂", "2. 消防防護計畫之檢討與更新", "3. 自衛消防編組與演練"],
    },
    "道路危險物品": {
        "law": "依道路交通管理處罰條例第29-2條 (初訓16-20小時) / 複訓每3年12-14小時",
        "purpose": "使道路危險物品運送人員具備正確之運送觀念及安全知識。",
        "outline_initial": ["1. 道路危險物品相關法規 (3小時)", "2. 危險物品分類、標示與標誌 (3小時)", "3. 運送車輛與容器之安全要求 (4小時)", "4. 緊急應變與事故處理 (3小時)", "5. 案例分析 (3-7小時)"],
        "outline_retraining": ["1. 道路危險物品法規更新", "2. 危險物品分類與標示複習", "3. 運送安全與緊急應變"],
    },
    "職安卡": {
        "law": "依職業安全衛生教育訓練規則第17條 (一般安全衛生教育訓練,新雇勞工 6 小時以上)",
        "purpose": "使勞工具備基本之職業安全衛生概念及自我保護能力,符合職安卡發證要求。",
        "outline_initial": ["1. 職業安全衛生概論與法規 (1.5小時)", "2. 個人防護具之使用與維護 (1小時)", "3. 危害辨識與作業安全 (1.5小時)", "4. 緊急應變與急救基本知識 (1小時)", "5. 健康促進與職業病預防 (1小時)"],
        "outline_retraining": ["1. 職業安全衛生法規更新", "2. 危害辨識複習", "3. 個人防護具與急救"],
    },
}


def get_course_outline(course_name, hours="3", category="複訓"):
    sorted_keys = sorted(COURSE_TEMPLATES.keys(), key=len, reverse=True)
    matched = None
    for keyword in sorted_keys:
        if keyword in course_name:
            matched = COURSE_TEMPLATES[keyword]
            break
    if not matched:
        return {
            "law": "依職業安全衛生教育訓練規則相關規定辦理",
            "purpose": "使作業人員具備相關安全衛生知識及技能",
            "outline": [f"請參考協會提供之課程簡章 (預計 {hours} 小時)"],
        }
    use_initial = category == "初訓"
    if not use_initial:
        try:
            if int(hours) >= 12:
                use_initial = True
        except (ValueError, TypeError):
            pass
    return {
        "law": matched["law"],
        "purpose": matched["purpose"],
        "outline": matched["outline_initial"] if use_initial else matched["outline_retraining"],
    }


# ==========================================================================
# 解析器
# ==========================================================================
class TichaScraper:
    name = "台灣省工商安全衛生協會"
    code = "ticsha"
    branches = {
        "中壢": "https://cli.ticsha.org.tw/course_list",
        "桃園": "https://tyn.ticsha.org.tw/course_list",
        "新竹": "https://hsz.ticsha.org.tw/course_list",
    }
    
    # 進度追蹤 (給前端讀取)
    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
  
    @classmethod
    def get_progress(cls):
        return dict(cls._progress)
    
    @classmethod
    def scrape(cls, fetch_details=False):
        """Commit 18d: 抓列表 + 詳細頁(學科/術科)。第二次跑有 cache 會快很多。"""
        cls._progress = {"stage": "list", "current": 0, "total": len(cls.branches), "message": "正在抓取課程列表..."}
        all_courses = []
        seen = set()

        for i, (branch_name, base_url) in enumerate(cls.branches.items()):
            try:
                cls._progress["current"] = i + 1
                cls._progress["message"] = f"抓取 {branch_name} 分會列表..."
                print(f"  抓取 {branch_name} 分會...")
                courses = cls._parse_page(base_url, branch_name)
                new_count = 0
                for c in courses:
                    if c["id"] not in seen:
                        seen.add(c["id"])
                        all_courses.append(c)
                        new_count += 1
                print(f"    → 找到 {new_count} 筆")
            except Exception as e:
                print(f"    ✗ 失敗: {e}")

        # === Commit 18d: 啟用 detail 抓取(學科 / 術科地址)===
        cls._fetch_all_details(all_courses)
        # ====================================================

        cls._progress = {"stage": "done", "current": 0, "total": 0, "message": "完成"}
        return all_courses
    
    @classmethod
    def _fetch_all_details(cls, courses):
        """Commit 18d: 抓詳細地址 + cache lookup + 並行(原本是序列,現在並行+跳 cache)。"""
        from concurrent.futures import ThreadPoolExecutor

        # === Commit 18d: cache 查詢(跳過已抓過的 detail)===
        cache = getattr(cls, "_cache", {}) or {}
        force_refresh = getattr(cls, "_force_refresh", False)

        targets_to_fetch = []
        cache_hit_count = 0
        for course in courses:
            if not course.get("course_id"):
                continue
            cached = cache.get(course["id"])
            if not force_refresh and cached and cached.get("location"):
                # Cache hit:直接複製,跳過 detail
                course["location"] = cached["location"]
                cache_hit_count += 1
            else:
                targets_to_fetch.append(course)

        print(f"  [Ticsha] Cache hit: {cache_hit_count} 筆(跳過 detail),需抓 detail: {len(targets_to_fetch)} 筆")
        # =====================================================

        cls._progress = {"stage": "details", "current": 0, "total": len(targets_to_fetch), "message": "Ticsha 抓詳細地址..."}

        def _grab(course):
            detail_addr = cls._fetch_detail(course)
            if detail_addr:
                course["location"] = detail_addr
            else:
                # 抓不到 detail 就 fallback 用分會固定地址
                course["location"] = BRANCH_ADDRESSES.get(course["branch"], f"{course['branch']} (詳洽協會)")
            return course

        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, _ in enumerate(pool.map(_grab, targets_to_fetch)):
                cls._progress["current"] = i + 1
                cls._progress["message"] = f"Ticsha 抓詳細地址 ({i+1}/{len(targets_to_fetch)})"
    
    @classmethod
    def _fetch_detail(cls, course):
        """抓單一課程的詳細頁面,取出乾淨的上課地址"""
        try:
            cid = course.get("course_id", "")
            if not cid:
                return None
            
            base_domain = course["url"].split("/course_list")[0]
            detail_urls = [
                f"{base_domain}/@@course_list?course_detail&id={cid}",
                f"{base_domain}/@@course_list?id={cid}",
            ]
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
                "X-Requested-With": "XMLHttpRequest",
            }
            
            for url in detail_urls:
                try:
                    resp = requests.get(url, headers=headers, timeout=10)
                    resp.encoding = "utf-8"
                    text = resp.text
                    
                    # 模式 1: 「上課地址：xxx」這種文字 (只取地址,到「樓」或「號」停止)
                    m = re.search(r"上課地[址點][:：]\s*([^<\n]{5,200})", text)
                    if m:
                        addr = m.group(1).strip()
                        # 砍掉電話、Email、傳真、教室、TEL 等雜訊 (出現這些字就截斷)
                        for stop_word in [" 0", " TEL", " Tel", " tel", "電話", "傳真", "Email", "email", " /", "yuda", "教室", "@"]:
                            idx = addr.find(stop_word)
                            if idx > 0:
                                addr = addr[:idx]
                        addr = re.sub(r"\s+", " ", addr).strip()
                        # 確認是有效地址 (至少含「市」「區」「路/街」「號/樓」)
                        if re.search(r"[市縣].+[區市].+(號|樓|街|路)", addr) and len(addr) < 60:
                            return addr
                    
                    # 模式 2: 找 HTML 中的純地址 pattern
                    soup = BeautifulSoup(text, "lxml")
                    for tag in soup.find_all(["td", "li", "p", "span", "div"]):
                        t = tag.get_text(strip=True)
                        # 純地址 pattern (市區+路+號)
                        if re.match(r"^[\u4e00-\u9fff]+[市縣][\u4e00-\u9fff]+區.{3,40}[號樓]$", t):
                            if len(t) < 60:
                                return t
                except Exception:
                    continue
            return None
        except Exception:
            return None
    
    @classmethod
    def _parse_page(cls, url, branch):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        base_domain = "https://" + url.split("/")[2]
        courses = []
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 8:
                    continue
                try:
                    seq = tds[0].get_text(strip=True)
                    if not seq.isdigit():
                        continue
                    name = tds[1].get_text(strip=True)
                    start_date = tds[2].get_text(strip=True)
                    end_date = tds[3].get_text(strip=True)
                    class_type = tds[4].get_text(strip=True)
                    hours = tds[6].get_text(strip=True)
                    fee = tds[7].get_text(strip=True)
                    status = tds[8].get_text(strip=True) if len(tds) > 8 else ""
                    
                    category = "未知"
                    course_id = ""
                    register_url = ""
                    for a in tr.find_all("a", href=True):
                        href = a["href"]
                        if "reg_course" in href and "category=" in href:
                            if "category=initial_training" in href:
                                category = "初訓"
                            elif "category=retraining" in href:
                                category = "複訓"
                            m = re.search(r"id=(\d+)", href)
                            if m:
                                course_id = m.group(1)
                            register_url = href if href.startswith("http") else base_domain + href
                            break
                    # commit 16: 額滿課可能沒 reg_course → 改用 brochure 或列表頁
                    if not course_id or not register_url:
                        for a in tr.find_all("a", href=True):
                            if "download_brochure" in a["href"]:
                                href = a["href"]
                                if not course_id:
                                    mid = re.search(r"id=(\d+)", href)
                                    if mid:
                                        course_id = mid.group(1)
                                if not register_url:
                                    register_url = href if href.startswith("http") else base_domain + href
                                break
                    if not register_url:
                        register_url = url
                    if category == "未知":
                        try:
                            h = int(hours)
                            if h <= 6:
                                category = "複訓"
                            elif h >= 12:
                                category = "初訓"
                        except (ValueError, TypeError):
                            pass
                    
                    nationality = "本國籍"
                    for nat in ["越南籍", "印尼籍", "菲律賓籍", "泰國籍"]:
                        if nat in name:
                            nationality = nat
                            break
                    
                    # 推測上課時間 (依班別)
                    class_time = ""
                    if "日間" in class_type:
                        class_time = "上午 9:00 - 下午 17:00"
                    elif "夜間" in class_type:
                        class_time = "晚上 18:30 - 21:30"
                    elif "假日" in class_type:
                        class_time = "上午 9:00 - 下午 17:00"
                    
                    # 上課地點 (依分會)
                    location = BRANCH_ADDRESSES.get(branch, f"{branch} (詳洽協會)")
                    
                    courses.append({
                        "id": f"{cls.code}_{branch}_{course_id or seq + '_' + start_date}",
                        "course_id": course_id,
                        "institute": cls.name, "branch": branch, "category": category,
                        "nationality": nationality, "name": name, "start_date": start_date + (f"({_wt[0]})" if (_wt := locals().get('weekday_time', '')) and '\u4e00' <= _wt[0] <= '\u9fff' else ""),
                        "end_date": end_date, "class_type": class_type, "class_time": class_time,
                        "location": location, "hours": hours, "fee": fee, "status": status,
                        "url": url, "register_url": register_url,
                    })
                except (IndexError, ValueError):
                    continue
        return courses


# =========================================================
# 解析器 #2:中國生產力中心 (CPC)
# =========================================================
class CPCScraper:
    name = "中國生產力中心"
    code = "cpc"
    base_url = "https://store.cpc.org.tw"
    # 3 個類別:職安(110) + 消防(111) + 營建(112)
    categories = {
        "職安": 110,
        "消防": 111,
        "營建": 112,
    }
    # 只保留這 2 個地區
    target_regions = {"桃園", "台北"}

    # 進度追蹤 (給前端讀取)
    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}

    @classmethod
    def get_progress(cls):
        return dict(cls._progress)

    @classmethod
    def _make_session(cls):
        import requests
        s = requests.Session(); s.verify = False
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/118.0.0.0 Safari/537.36"),
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        return s

    @classmethod
    def _fetch_page(cls, session, cat_id, page=1):
        import re as _re
        from datetime import datetime as _dt, timedelta as _td; _t = _dt.now(); url = f"{cls.base_url}/Train/Category/{cat_id}?StartDate={_t.strftime('%Y%m%d')}&EndDate={(_t + _td(days=90)).strftime('%Y%m%d')}"
        if page > 1:
            url += f"&page={page}"
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.content, "html.parser")

    @classmethod
    def _last_page(cls, soup):
        import re as _re
        pag = soup.find("ul", class_="pagination")
        if not pag:
            return 1
        for a in pag.find_all("a"):
            if "最後一頁" in a.get_text():
                m = _re.search(r"page=(\d+)", a.get("href", ""))
                if m:
                    return int(m.group(1))
        return 1

    @classmethod
    def _parse_row(cls, tr):
        import re as _re
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            return None
        region_badge = tds[0].find("span", class_="badge")
        if not region_badge:
            return None
        region = region_badge.get_text(strip=True)

        status_badge = tds[1].find("span", class_="badge")
        status = status_badge.get_text(strip=True) if status_badge else ""

        a = tds[1].find("a")
        if not a:
            return None
        link = a.get("href", ""); link = "https://store.cpc.org.tw" + link if link.startswith("/Train/") else link
        title_attr = a.get("title", "").strip()
        m = _re.match(r"^(\S+)\s+(.+)$", title_attr)
        if m:
            code_, name = m.group(1), m.group(2)
        else:
            code_, name = "", title_attr

        small = a.find("small")
        subtitle = " ".join(small.get_text(separator=" ", strip=True).split()) if small else ""

        deadline = ""
        for s in tds[1].find_all("small"):
            txt = s.get_text(strip=True)
            if "報名截止日" in txt:
                dm = _re.search(r"(\d{4}-\d{2}-\d{2})", txt)
                if dm:
                    deadline = dm.group(1)

        hours = tds[2].get_text(strip=True)

        td4_text = " ".join(tds[3].get_text(separator=" ", strip=True).split())
        dm = _re.match(r"(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})\s*(?:\(([^)]+)\))?", td4_text)
        if dm:
            start_date = dm.group(1)
            end_date = dm.group(2)
            weekday_time = (dm.group(3) or "").strip()
        else:
            start_date = end_date = ""
            weekday_time = td4_text

        full = name + " " + subtitle + " " + weekday_time
        time_m = _re.search(r"(\d{1,2}):\d{2}", weekday_time)
        if "夜" in full:
            day_type = "夜間班"
        elif time_m and int(time_m.group(1)) >= 17:
            day_type = "夜間班"
        elif "假日" in full or any(d in weekday_time for d in ["六", "日"]):
            day_type = "假日班"
        else:
            day_type = "日間班"

        nm = name + subtitle
        if "回訓" in nm or "在職" in nm or "複訓" in nm:
            category = "複訓"
        elif "初訓" in nm:
            category = "初訓"
        else:
            category = "—"

        if any(kw in name for kw in ("移工", "外籍", "越南", "菲律賓", "印尼", "泰")):
            nationality = "外籍"
        else:
            nationality = "本國籍"

        display_name = f"{name} {subtitle}".strip() if subtitle else name

        return {
            "institute": "中國生產力中心", "id": f"cpc-{code_}",
            "code": code_,
            "name": display_name,
            "branch": region,
            "category": category,
            "nationality": nationality,
            "start_date": start_date + (f"({_wt[0]})" if (_wt := locals().get('weekday_time', '')) and '\u4e00' <= _wt[0] <= '\u9fff' else ""),
            "end_date": end_date,
            "class_time": _re.sub(r"(\d+):(\d+)-(\d+):(\d+)", lambda m: f"{'上午' if int(m.group(1))<12 else '下午'} {int(m.group(1))}:{m.group(2)} - {'上午' if int(m.group(3))<12 else '下午'} {int(m.group(3))}:{m.group(4)}", _re.sub(r"^\D+", "", weekday_time)),
            "class_type": day_type,
            "hours": hours,
            "fee": "",
            "status": status,
            "deadline": deadline,
            "register_url": link,
            "location": "",
            "source": "cpc",
        }

    @classmethod
    def _parse_detail(cls, html):
        """Commit 18b: 學科 / 術科 分開抓,合併時用換行符號連接。"""
        soup = BeautifulSoup(html, "html.parser")
        fee = ""

        # === 學科 / 術科 分開抓 ===
        _loc_xueke = ""   # 學科
        _loc_shuke = ""   # 術科

        # Strategy A: <tr>/<th>/<td> 結構優先
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            if not th:
                continue
            th_text = th.get_text(strip=True)
            td = tr.find("td")
            if not td:
                continue
            td_text = " ".join(td.get_text(separator=" ", strip=True).split())

            if "學科上課地點" in th_text or "學科上課地址" in th_text:
                _loc_xueke = td_text
            elif "術科上課地點" in th_text or "術科上課地址" in th_text:
                _loc_shuke = td_text
            elif ("上課地點" in th_text or "上課地址" in th_text) and not _loc_xueke and not _loc_shuke:
                # 沒分學科 / 術科時當作學科
                _loc_xueke = td_text

        # Strategy B: 全文 regex fallback(網站可能改版)
        if not _loc_xueke or not _loc_shuke:
            body_text = soup.get_text(separator="\n", strip=True)
            if not _loc_xueke:
                m = re.search(r"學科上課地[址點][:\uff1a]?\s*([^\n]+)", body_text)
                if m:
                    _loc_xueke = m.group(1).strip()
            if not _loc_shuke:
                m = re.search(r"術科上課地[址點][:\uff1a]?\s*([^\n]+)", body_text)
                if m:
                    _loc_shuke = m.group(1).strip()
            # 都沒抓到 → 用一般「上課地點」當 fallback
            if not _loc_xueke and not _loc_shuke:
                m = re.search(r"上課地[址點][:\uff1a]?\s*([^\n]+)", body_text)
                if m:
                    _loc_xueke = m.group(1).strip()

        # 合併(換行格式,儲存時用 \n,信件顯示時會轉 <br>)
        if _loc_xueke and _loc_shuke and _loc_xueke != _loc_shuke:
            address = f"學科:{_loc_xueke}\n術科:{_loc_shuke}"
        elif _loc_xueke:
            address = _loc_xueke
        elif _loc_shuke:
            address = _loc_shuke
        else:
            address = ""
        # ========================================

        # 費用
        ps = soup.select_one("span.text-red.lead")
        if ps:
            fee = ps.get_text(strip=True).replace(",", "")
        return address, fee

    @classmethod
    def scrape(cls, fetch_details=True):
        """抓 CPC 3 個類別,自動過濾桃園+台北,並補上詳細頁的地址+費用。"""
        from concurrent.futures import ThreadPoolExecutor

        session = cls._make_session()
        cls._progress = {"stage": "list", "current": 0, "total": 0, "message": "CPC 正在掃描列表..."}

        # 階段 1:預掃每個類別第 1 頁,得知總頁數
        page_jobs = []  # 元素是 (cat_id, page_num, 預抓好的 soup 或 None)
        for cat_name, cat_id in cls.categories.items():
            try:
                soup1 = cls._fetch_page(session, cat_id, 1)
                last = cls._last_page(soup1)
                page_jobs.append((cat_id, 1, soup1))
                for p in range(2, last + 1):
                    page_jobs.append((cat_id, p, None))
                print(f"  [CPC] {cat_name}(類別 {cat_id}) 共 {last} 頁")
            except Exception as e:
                print(f"  [CPC] 抓 {cat_name} 失敗: {e}")

        cls._progress["total"] = len(page_jobs)

        # 階段 2:並行抓所有列表頁,套地區過濾
        all_courses = []
        seen_ids = set()

        def grab_page(job):
            cat_id, p, prefetched = job
            try:
                soup = prefetched if prefetched is not None else cls._fetch_page(session, cat_id, p)
                rows = []
                table = soup.find("table", class_="table table-hover")
                if table:
                    for tr in table.find_all("tr")[1:]:  # 跳過表頭
                        c = cls._parse_row(tr)
                        if c and any(r in c["branch"] for r in cls.target_regions):
                            rows.append(c)
                return rows
            except Exception as e:
                print(f"  [CPC] page {cat_id}/{p} 失敗: {e}")
                return []

        with ThreadPoolExecutor(max_workers=6) as pool:
            for idx, page_rows in enumerate(pool.map(grab_page, page_jobs)):
                cls._progress["current"] = idx + 1
                cls._progress["message"] = f"CPC 掃描列表 {idx+1}/{len(page_jobs)}..."
                for c in page_rows:
                    if c["id"] not in seen_ids:
                        seen_ids.add(c["id"])
                        all_courses.append(c)

        # 階段 3:對篩選後的課程抓詳細頁拿地址+費用(Commit 18b: cache 加速)
        if fetch_details and all_courses:
            # === Commit 18b: cache 查詢(跳過已抓過的 detail)===
            cache = getattr(cls, "_cache", {}) or {}
            force_refresh = getattr(cls, "_force_refresh", False)

            targets_to_fetch = []
            cache_hit_count = 0
            for course in all_courses:
                cached = cache.get(course["id"])
                if not force_refresh and cached and cached.get("location"):
                    # Cache hit:直接複製 location + fee,跳過 detail 頁
                    course["location"] = cached.get("location", "")
                    course["fee"] = cached.get("fee", "")
                    cache_hit_count += 1
                else:
                    # Cache miss(新課 / 上次失敗 / 強制全抓):加入待抓清單
                    targets_to_fetch.append(course)

            print(f"  [CPC] Cache hit: {cache_hit_count} 筆(跳過 detail),需抓 detail: {len(targets_to_fetch)} 筆")
            # ===================================================

            cls._progress = {
                "stage": "details", "current": 0, "total": len(targets_to_fetch),
                "message": f"抓 CPC 課程詳細資料 0/{len(targets_to_fetch)}..."
            }

            def grab_detail(course):
                try:
                    r = session.get(course["register_url"], timeout=20)
                    addr, fee = cls._parse_detail(r.text)
                    course["location"] = addr
                    course["fee"] = fee
                except Exception as e:
                    print(f"  [CPC] detail {course['code']} 失敗: {e}")
                return course

            with ThreadPoolExecutor(max_workers=6) as pool:
                for idx, _ in enumerate(pool.map(grab_detail, targets_to_fetch)):
                    cls._progress["current"] = idx + 1
                    cls._progress["message"] = f"抓 CPC 詳細 {idx+1}/{len(targets_to_fetch)}..."

        cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
        print(f"  [CPC] 完成,共 {len(all_courses)} 堂課(桃園+台北)")
        return all_courses

# =========================================================
# 解析器 #3:中華民國工業安全衛生協會 (ISHA)
# =========================================================
class ISHAScraper:
    name = "中華民國工業安全衛生協會"
    code = "isha"
    base_url = "https://isha.org.tw"
    list_url = "https://isha.org.tw/Msite/tech/serch.aspx"
    detail_url_tpl = "https://isha.org.tw/Msite/tech/serch_inner.aspx?WorkLogID={}"

    # 北區 5 站對應到的簡稱
    # 注意:「中壢」必須在「桃園」之前(因為「桃園職業訓練中心(中壢教室)」兩個關鍵字都會匹配)
    NORTH_STATIONS = (
        ("中壢", "中壢"),
        ("台北", "台北"),
        ("新北", "新北"),
        ("桃園", "桃園"),
        ("新竹", "新竹"),
    )

    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
    _debug_counter = 0

    @classmethod
    def get_progress(cls):
        return dict(cls._progress)

    @classmethod
    def _make_session(cls):
        import urllib3 as _isha_urllib3
        _isha_urllib3.disable_warnings(_isha_urllib3.exceptions.InsecureRequestWarning)
        s = requests.Session()
        s.verify = False
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/118.0.0.0 Safari/537.36"),
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        return s

    @classmethod
    def _vs(cls, soup):
        d = {}
        for n in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            i = soup.find("input", {"name": n})
            d[n] = i.get("value", "") if i else ""
        return d

    @classmethod
    def _roc_to_west(cls, s):
        m = re.match(r"^(\d{3})(\d{2})(\d{2})$", str(s).strip())
        if not m:
            return ""
        y, mo, d = m.groups()
        return f"{int(y) + 1911:04d}-{mo}-{d}"

    @classmethod
    def _find_next_page_target(cls, soup, current_page):
        # Strategy 1: BT_<current+1>
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(rf"__doPostBack\('([^']*\$BT_{current_page + 1})'", onclick)
            if pm:
                return pm.group(1), current_page + 1
        # Strategy 2: smallest BT_N > current
        next_n = None
        next_t = None
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('([^']*\$BT_(\d+))'", onclick)
            if pm:
                n = int(pm.group(2))
                if n > current_page:
                    if next_n is None or n < next_n:
                        next_n = n
                        next_t = pm.group(1)
        if next_t:
            return next_t, next_n
        # Strategy 3: BT_right (ISHA 下一頁 icon)
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('([^']*\$BT_right)'", onclick)
            if pm:
                return pm.group(1), current_page + 1
        # Strategy 4: BT_end (跳下一批)
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('([^']*\$BT_end)'", onclick)
            if pm:
                return pm.group(1), current_page + 1
        # Strategy 5: 含「下一頁/>/Next」文字
        for tag in soup.find_all(["a", "input"]):
            text = (tag.get_text(strip=True) or "") + " " + \
                   str(tag.get("value", "")) + " " + str(tag.get("title", ""))
            if any(kw in text for kw in (">", "下一頁", "下頁", "Next", "next", "→")):
                onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
                pm = re.search(r"__doPostBack\('([^']+)'", onclick)
                if pm:
                    target = pm.group(1)
                    if f"BT_{current_page}" in target or target.endswith("BT_1"):
                        continue
                    return target, current_page + 1
        return None, None

    # -----------------------------------------------------
    # 列表頁解析
    # -----------------------------------------------------
    @classmethod
    def _parse_list(cls, soup):
        results = []
        seen_wid = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            wid_m = re.search(r"WorkLogID=([A-Za-z0-9]+)", href)
            if not wid_m:
                continue
            wid = wid_m.group(1)
            if wid in seen_wid:
                continue
            seen_wid.add(wid)

            full = " ".join(a.get_text(" ", strip=True).split())
            if not full:
                continue

            # status — v7: 加「預定開課」
            name = full
            status = ""
            first_idx = len(full)
            for _isha_st in ("確定開課", "預定開課", "招生中", "已截止", "已額滿", "完成報名"):
                idx = full.find(_isha_st)
                if 0 <= idx < first_idx:
                    first_idx = idx
                    status = _isha_st
            if status:
                name = full[:first_idx].strip()

            ct_m = re.search(r"(日間班|夜間班|假日班)", full)
            class_type = ct_m.group(1) if ct_m else ""

            hours = ""
            hr_m = re.search(r"課程時數[:：]?\s*(\d+\.?\d*)", full)
            if hr_m:
                hours = hr_m.group(1)

            cat_raw = ""
            cm = re.search(r"初/在職[:：]?\s*(\S+?)(?=\s|開課|課程|$)", full)
            if cm:
                cat_raw = cm.group(1)

            start_date = ""
            end_date = ""
            dr_m = re.search(r"開課日期[:：]?\s*(\d{7})\s*[-~]\s*(\d{7})", full)
            if dr_m:
                start_date = cls._roc_to_west(dr_m.group(1))
                end_date = cls._roc_to_west(dr_m.group(2))
            else:
                ds_m = re.search(r"開課日期[:：]?\s*(\d{7})", full)
                if ds_m:
                    start_date = cls._roc_to_west(ds_m.group(1))

            fee = ""
            fe_m = re.search(r"課程費用[:：]?\s*([\d,]+)\s*元", full)
            if fe_m:
                fee = fe_m.group(1).replace(",", "")

            if href.startswith("http"):
                reg = href
            else:
                reg = cls.base_url + ("" if href.startswith("/") else "/") + href

            nat = "外國籍" if any(k in name for k in
                ("越南", "印尼", "菲律賓", "泰國", "外籍", "外國", "Vietnam", "Indonesia")
            ) else "本國籍"

            if "初訓" in cat_raw:
                category = "初訓"
            elif "在職" in cat_raw or "回訓" in cat_raw:
                category = "複訓"
            else:
                category = ""  # v7: 留空,讓詳細頁覆蓋

            if not class_type:
                if "夜" in name:
                    class_type = "夜間班"
                elif "假日" in name or "週六" in name or "週日" in name:
                    class_type = "假日班"
                else:
                    class_type = "日間班"

            results.append({
                "institute": cls.name,
                "id": f"isha-{wid}",
                "code": wid,
                "name": name,
                "branch": "",
                "category": category,
                "nationality": nat,
                "start_date": start_date,
                "end_date": end_date,
                "class_type": class_type,
                "class_time": "",
                "hours": hours,
                "fee": fee,
                "status": status,
                "deadline": "",
                "register_url": reg,
                "location": "",
                "source": "isha",
                "work_log_id": wid,
            })
        return results

    # -----------------------------------------------------
    # 詳細頁解析 (v7: 含重試 + 多欄位 fallback)
    # -----------------------------------------------------
    @classmethod
    def _fetch_detail(cls, session, course):
        try:
            wid = course.get("work_log_id") or course.get("code", "")
            if not wid:
                return
            url = cls.detail_url_tpl.format(wid)

            # v7: 1 次重試
            r = None
            for attempt in range(2):
                try:
                    r = session.get(url, timeout=20)
                    r.encoding = "utf-8"
                    break
                except Exception:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    raise
            if r is None:
                return

            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)

            if cls._debug_counter < 3:
                cls._debug_counter += 1
                idx = cls._debug_counter
                print(f"  [ISHA DEBUG#{idx}] wid={wid} HTTP={r.status_code} text_len={len(text)}")
                preview = text[:800].replace("\n", " ⏎ ")
                print(f"  [ISHA DEBUG#{idx}] text[:800]={preview!r}")

            # === 站別 (v8: 嚴格優先「開課站別」,Pattern C 防呆) ===
            raw_station = ""
            # Pattern A (嚴格): 優先「開課站別:」
            if (m := re.search(r"開課站別\s*[:：]\s*([^\n]+)", text)):
                raw_station = m.group(1).strip()
            # Pattern A2 (寬鬆): 一般「站別:」
            elif (m := re.search(r"站別\s*[:：]\s*([^\n]+)", text)):
                raw_station = m.group(1).strip()
            # Pattern B: 「站別」標籤後 3 行內找站別值
            if not raw_station:
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if "站別" in line:
                        for j in range(i + 1, min(i + 4, len(lines))):
                            cand = lines[j].strip()
                            if cand and ("職訓" in cand or "職業訓練" in cand or "服務處" in cand or "教室" in cand):
                                raw_station = cand
                                break
                        if raw_station:
                            break
            # Pattern C (v8: 防呆 — 只有當文本完全沒「站別」字眼才fallback,避免誤抓)
            if not raw_station and "站別" not in text:
                for kw in ("台北職訓中心", "新北職業訓練中心", "桃園職業訓練中心",
                           "新竹區職業訓練中心", "台中職業訓練中心", "彰化區職業訓練中心",
                           "雲林職訓中心", "高雄服務處", "台南職訓中心", "台中職訓"):
                    if kw in text:
                        raw_station = kw
                        break
            course["_raw_station"] = raw_station
            for short, alias in cls.NORTH_STATIONS:
                if alias in raw_station:
                    course["branch"] = short
                    break

            # === 開課日期/結業日期 (v7 新增) ===
            if not course.get("start_date"):
                date_section = re.search(r"課程日期(.*?)(?=課程時數|時段|報名截止|電話|學科上課|備註|$)", text, re.DOTALL)
                if date_section:
                    dates = re.findall(r"(\d{3})年(\d{1,2})月(\d{1,2})日", date_section.group(1))
                    if dates:
                        y, mo, d = dates[0]
                        course["start_date"] = f"{int(y)+1911:04d}-{int(mo):02d}-{int(d):02d}"
                        if len(dates) >= 2:
                            y, mo, d = dates[1]
                            course["end_date"] = f"{int(y)+1911:04d}-{int(mo):02d}-{int(d):02d}"

            # === 課程時數 (v7 新增 — 從詳細頁補) ===
            if not course.get("hours"):
                if (h := re.search(r"課程時數\s*[:：]?\s*(\d+\.?\d*)", text)):
                    course["hours"] = h.group(1)

            # === 初/在職 → category (v7 新增 — 從詳細頁補) ===
            if not course.get("category"):
                if (c := re.search(r"初/在職\s*[:：]?\s*(\S+)", text)):
                    cat = c.group(1)
                    if "初訓" in cat:
                        course["category"] = "初訓"
                    elif "在職" in cat or "回訓" in cat:
                        course["category"] = "複訓"

            # === 課程費用 ===
            if not course.get("fee"):
                if (_isha_fee := re.search(r"課程費用\s*[:：]?\s*([\d,]+)\s*元?", text)):
                    course["fee"] = _isha_fee.group(1).replace(",", "")

            # === 上課地址 (v8: 修 typo 地點→地址 + 同時抓學科 & 術科) ===
            _loc_xueke = ""   # 學科
            _loc_shuke = ""   # 術科
            if (m := re.search(r"學科上課地址\s*[:：]\s*([^\n]+)", text)):
                _loc_xueke = m.group(1).strip()
            if (m := re.search(r"術科上課地址\s*[:：]\s*([^\n]+)", text)):
                _loc_shuke = m.group(1).strip()
            # 萬一網站改字眼,備用 fallback (地點)
            if not _loc_xueke and (m := re.search(r"學科上課地點\s*[:：]\s*([^\n]+)", text)):
                _loc_xueke = m.group(1).strip()
            if not _loc_shuke and (m := re.search(r"術科上課地點\s*[:：]\s*([^\n]+)", text)):
                _loc_shuke = m.group(1).strip()
            # 通用 fallback
            if not _loc_xueke and not _loc_shuke:
                if (m := re.search(r"上課地[址點]\s*[:：]\s*([^\n]+)", text)):
                    _loc_xueke = m.group(1).strip()
            # 合併 (Commit 18c: 改用換行格式,跟 CPC 一致,信件會自動轉 <br>)
            if _loc_xueke and _loc_shuke and _loc_xueke != _loc_shuke:
                course["location"] = f"學科:{_loc_xueke}\n術科:{_loc_shuke}"
            elif _loc_xueke:
                course["location"] = _loc_xueke
            elif _loc_shuke:
                course["location"] = _loc_shuke

            # === 時段 → class_time ===
            for _isha_label in ("時段", "上課時間"):
                if course.get("class_time"):
                    break
                _isha_sec = re.search(rf"{_isha_label}\s*[:：]\s*([^\n]+)", text)
                if not _isha_sec:
                    continue
                raw = _isha_sec.group(1).strip()
                _tm = re.search(r"(\d{1,2})[:：]?(\d{2})\s*[-~]\s*(\d{1,2})[:：]?(\d{2})", raw)
                if _tm:
                    h1, m1, h2, m2 = (int(x) for x in _tm.groups())
                    if 0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59:
                        course["class_time"] = (
                            f"{'上午' if h1 < 12 else '下午'} {h1}:{m1:02d} - "
                            f"{'上午' if h2 < 12 else '下午'} {h2}:{m2:02d}"
                        )

            # === 報名截止 ===
            if (_isha_dl := re.search(r"報名截止日期\s*[:：]?\s*(\d{3})年(\d{1,2})月(\d{1,2})日", text)):
                y, mo, d = _isha_dl.groups()
                course["deadline"] = f"{int(y) + 1911:04d}-{int(mo):02d}-{int(d):02d}"

            # === class_time fallback (v7 新增 — 按 班別 給預設,或顯示「未定」) ===
            if not course.get("class_time"):
                ct = course.get("class_type", "")
                if "夜" in ct:
                    course["class_time"] = "晚上 18:30 - 21:30"
                elif "假日" in ct or "日間" in ct:
                    course["class_time"] = "上午 9:00 - 下午 17:00"
                else:
                    course["class_time"] = "未定,待協會正式通知"

            # === category fallback (v7 新增) ===
            if not course.get("category"):
                # 用 hours 猜:>=12 hr 通常是初訓
                try:
                    if float(course.get("hours") or 0) >= 12:
                        course["category"] = "初訓"
                    else:
                        course["category"] = "複訓"
                except (ValueError, TypeError):
                    course["category"] = "—"

        except Exception as e:
            print(f"  [ISHA] detail {course.get('code', '?')} 失敗: {e}")

    # -----------------------------------------------------
    # 主流程
    # -----------------------------------------------------
    @classmethod
    def scrape(cls, fetch_details=True):
        from concurrent.futures import ThreadPoolExecutor

        cls._debug_counter = 0
        cls._progress = {"stage": "list", "current": 0, "total": 0, "message": "ISHA 載入..."}
        session = cls._make_session()

        try:
            r = session.get(cls.list_url, timeout=20)
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"  [ISHA] 初始 GET 失敗: {e}")
            cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
            return []

        max_page_seen = 1
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('[^']*\$BT_(\d+)'", onclick)
            if pm:
                n = int(pm.group(1))
                if n > max_page_seen:
                    max_page_seen = n
        print(f"  [ISHA] 初始頁能看到的最大頁碼: {max_page_seen}")
        cls._progress["total"] = max(max_page_seen, 60)

        all_rows = []
        seen_ids = set()

        def _absorb(page_num, page_soup):
            page_rows = cls._parse_list(page_soup)
            new = 0
            for c in page_rows:
                if c["id"] not in seen_ids:
                    seen_ids.add(c["id"])
                    all_rows.append(c)
                    new += 1
            print(f"  [ISHA] 第 {page_num} 頁: 解析 {len(page_rows)} 筆 (新加 {new})")
            return new

        _absorb(1, soup)

        current_page = 1
        max_iterations = 100
        consecutive_empty = 0

        while current_page < max_iterations:
            next_target, next_page = cls._find_next_page_target(soup, current_page)
            if not next_target:
                print(f"  [ISHA] 第 {current_page} 頁找不到下一頁,結束分頁")
                break

            try:
                vs = cls._vs(soup)
                form = {
                    "__EVENTTARGET": next_target,
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": vs["__VIEWSTATE"],
                    "__VIEWSTATEGENERATOR": vs["__VIEWSTATEGENERATOR"],
                    "__EVENTVALIDATION": vs["__EVENTVALIDATION"],
                }
                r = session.post(cls.list_url, data=form, timeout=25)
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")
                current_page = next_page
                cls._progress["current"] = current_page
                cls._progress["message"] = f"ISHA 列表 {current_page}/{cls._progress['total']}..."
                new = _absorb(current_page, soup)
                if new == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        print(f"  [ISHA] 連續 3 頁無新資料,停止分頁")
                        break
                else:
                    consecutive_empty = 0
            except Exception as e:
                print(f"  [ISHA] 第 {current_page + 1} 頁失敗: {e}")
                break

        print(f"  [ISHA] 列表抓完,共 {len(all_rows)} 筆")

        if fetch_details and all_rows:
            # === Commit 18c: cache 查詢(跳過已抓過的 detail)===
            cache = getattr(cls, "_cache", {}) or {}
            force_refresh = getattr(cls, "_force_refresh", False)

            # 要從 cache 複製的欄位(都是 detail 頁才有的欄位)
            _isha_cache_fields = ["branch", "start_date", "end_date", "hours", "category",
                                  "fee", "location", "class_time", "deadline"]

            targets_to_fetch = []
            cache_hit_count = 0
            for course in all_rows:
                cached = cache.get(course["id"])
                if not force_refresh and cached and cached.get("location"):
                    # Cache hit:複製 detail 頁抓的欄位,跳過 detail 抓取
                    for _field in _isha_cache_fields:
                        if cached.get(_field):
                            course[_field] = cached[_field]
                    cache_hit_count += 1
                else:
                    # Cache miss:加入待抓清單
                    targets_to_fetch.append(course)

            print(f"  [ISHA] Cache hit: {cache_hit_count} 筆(跳過 detail),需抓 detail: {len(targets_to_fetch)} 筆")
            # ==================================================

            cls._progress = {
                "stage": "details", "current": 0, "total": len(targets_to_fetch),
                "message": f"ISHA 抓詳細 0/{len(targets_to_fetch)}...",
            }

            def _isha_do(c):
                cls._fetch_detail(session, c)
                return c

            with ThreadPoolExecutor(max_workers=6) as pool:
                for idx, _ in enumerate(pool.map(_isha_do, targets_to_fetch)):
                    cls._progress["current"] = idx + 1
                    cls._progress["message"] = f"ISHA 抓詳細 {idx+1}/{len(targets_to_fetch)}..."

        filtered = [c for c in all_rows if c.get("branch") in ("台北", "新北", "桃園", "中壢", "新竹")]
        non_north_count = len(all_rows) - len(filtered)
        empty_count = sum(1 for c in all_rows if not c.get("branch"))

        print(f"  [ISHA] 站別範例 (前 5 筆):")
        for c in all_rows[:5]:
            print(f"    code={c['code']} raw_station={c.get('_raw_station', '(無)')!r} → branch={c.get('branch', '')!r}")
        print(f"  [ISHA] 北區 {len(filtered)} 筆 / 非北區或無 {non_north_count} 筆 / 空 {empty_count} 筆")

        for c in filtered:
            c.pop("_raw_station", None)

        cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
        print(f"  [ISHA] 完成,共 {len(filtered)} 堂課(北區 5 站)")
        return filtered


SCRAPERS = {    "ticsha": TichaScraper,
    "cpc": CPCScraper,
    "isha": ISHAScraper,
}

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"courses": [], "last_updated": None}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# === Commit 18a: cache 與 cutoff 設定 ===
CUTOFF_DAYS_PAST = 0      # 已開課的就過濾掉(同仁不能選過去的日期)
CUTOFF_DAYS_FUTURE = 180  # 只看未來半年內的課


def _build_cache_by_scraper(courses):
    """把上次的課程資料按 scraper code 分組,變成 {scraper_code: {course_id: course}}。"""
    cache = {}
    for c in courses:
        code = c.get("_scraper_code", "")
        if not code:
            continue
        cache.setdefault(code, {})[c.get("id", "")] = c
    return cache


def _is_in_cutoff_range(course, today=None):
    """檢查開課日是否在 [-CUTOFF_DAYS_PAST, +CUTOFF_DAYS_FUTURE] 範圍內。
    沒有 start_date 的保留(寬鬆),避免漏掉沒抓到日期的課。"""
    from datetime import date as _date, datetime as _dt
    sd = (course.get("start_date") or "").strip()
    if not sd:
        return True
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", sd)
    if not m:
        return True
    try:
        course_date = _dt.strptime(m.group(0), "%Y-%m-%d").date()
        today = today or _date.today()
        delta_days = (course_date - today).days
        return -CUTOFF_DAYS_PAST <= delta_days <= CUTOFF_DAYS_FUTURE
    except Exception:
        return True


def update_courses(codes, force_refresh=False):
    # === Commit 18a: 建立 cache(從上次抓過的資料)===
    old_data = load_data()
    if force_refresh:
        cache_by_scraper = {}
        print("⚡ 強制全抓模式:忽略 cache,所有課程都會重抓 detail")
    else:
        cache_by_scraper = _build_cache_by_scraper(old_data.get("courses", []))
        if cache_by_scraper:
            for ckey, cmap in cache_by_scraper.items():
                print(f"📦 Cache: {ckey} 有 {len(cmap)} 筆舊資料可用(detail 頁可跳過)")
        else:
            print("📦 Cache: 空(第一次跑或容器重啟,所有 detail 都會抓)")
    # ===============================================

    all_courses = [c for c in old_data.get("courses", []) if c.get("_scraper_code") not in codes]
    for code in codes:
        if code in SCRAPERS:
            scraper = SCRAPERS[code]
            # === Commit 18a: 把 cache 給 scraper(透過 class attribute,scraper 內部可選擇使用)===
            scraper._cache = cache_by_scraper.get(code, {})
            scraper._force_refresh = force_refresh
            # =================================================================================
            print(f"\n=== 更新 {scraper.name} ===")
            try:
                new_rows = [{**c, "_scraper_code": code} for c in scraper.scrape()]
                all_courses.extend(new_rows)
                # 增量儲存:每個協會跑完就先存一份,萬一後面爆掉至少保住前面
                data = {"courses": all_courses, "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                save_data(data)
                print(f"=== {scraper.name} 完成,已存檔(本次 {len(new_rows)} 筆 / 累計 {len(all_courses)} 筆) ===")
            except Exception as e:
                print(f"=== {scraper.name} 失敗: {e} ===")
                import traceback
                traceback.print_exc()

    # === Commit 18a: 課程 cutoff 過濾(只留 -7d ~ +180d)===
    before_filter = len(all_courses)
    all_courses = [c for c in all_courses if _is_in_cutoff_range(c)]
    after_filter = len(all_courses)
    if before_filter != after_filter:
        print(f"📅 Cutoff 過濾:{before_filter} → {after_filter} 筆 (-{CUTOFF_DAYS_PAST}d ~ +{CUTOFF_DAYS_FUTURE}d)")
    # ======================================================

    data = {"courses": all_courses, "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    save_data(data)
    return data


# ==========================================================================
# Flask App
# ==========================================================================
app = Flask(__name__)
# Commit 18e (緊急修復):固定 secret_key,避免 worker 重啟導致使用者被自動登出
# 優先讀環境變數 SECRET_KEY (建議到 Render 設),沒設就用內建備用值
app.secret_key = os.environ.get("SECRET_KEY", "training-tool-fixed-key-2026-jentechhr1-please-rotate-yearly")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登入", "redirect": "/login"}), 401
            return redirect(url_for("login_page"))
        # 更新在線狀態 (用 username + IP 區分)
        update_online(session["user"]["username"], request.remote_addr or "?")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        force = request.form.get("force", "") == "1"  # 強制踢掉舊登入
        
        user = verify_user(username, password)
        if not user:
            return render_template_string(LOGIN_TEMPLATE, error="帳號或密碼錯誤", show_force=False)
        
        # 檢查該帳號是否已有人在線 (在不同 IP)
        my_ip = request.remote_addr or "?"
        existing = []
        with ONLINE_LOCK:
            for key in list(ONLINE_USERS.keys()):
                u, ip = key.split("|", 1) if "|" in key else (key, "?")
                if u == username and ip != my_ip and ONLINE_USERS[key] > time.time() - 300:
                    existing.append(ip)
        
        if existing and not force:
            return render_template_string(LOGIN_TEMPLATE, 
                error=f"⚠️ 此帳號目前已有人從 {existing[0]} 登入。若要強制踢掉並使用本帳號,請按下方「強制登入」",
                show_force=True, _username=username, _password=password)
        
        # 如果是強制登入,踢掉所有其他 IP 的這個帳號
        if force:
            with ONLINE_LOCK:
                for key in list(ONLINE_USERS.keys()):
                    u, ip = key.split("|", 1) if "|" in key else (key, "?")
                    if u == username:
                        del ONLINE_USERS[key]
        
        session["user"] = {
            "username": user["username"],
            "role": user["role"],
            "display_name": user["display_name"],
            "login_time": time.time(),
        }
        update_online(user["username"], my_ip)
        return redirect(url_for("index"))
    
    return render_template_string(LOGIN_TEMPLATE, error=None, show_force=False)


@app.route("/logout")
def logout():
    if "user" in session:
        remove_online(session["user"]["username"], request.remote_addr or "?")
    session.pop("user", None)
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    return render_template_string(HTML_TEMPLATE, user=session["user"])


@app.route("/api/courses")
@login_required
def api_courses():
    return jsonify(load_data())


@app.route("/api/online")
@login_required
def api_online():
    return jsonify({"users": get_online_users(), "count": len(get_online_users())})


@app.route("/api/fetch_location", methods=["POST"])
@login_required
def api_fetch_location():
    """抓取單一課程的詳細上課地點 (在使用者勾選後才抓,不是全抓)"""
    body = request.get_json() or {}
    course = body.get("course", {})
    
    if not course.get("course_id"):
        return jsonify({"location": None, "error": "無 course_id"})
    
    # 補充 url 欄位 (給 _fetch_detail 用)
    if not course.get("url"):
        course["url"] = f"https://cli.ticsha.org.tw/course_list"
    
    location = TichaScraper._fetch_detail(course)
    return jsonify({"location": location or ""})


@app.route("/api/update_progress")
@login_required
def api_update_progress():
    """查詢更新進度,給前端 polling"""
    return jsonify(TichaScraper.get_progress())


@app.route("/api/update", methods=["POST"])
@login_required
def api_update():
    body = request.get_json() or {}
    codes = body.get("scrapers", ["ticsha"])
    force_refresh = bool(body.get("force_refresh", False))
    data = update_courses(codes, force_refresh=force_refresh)
    return jsonify({"ok": True, "count": len(data["courses"]), "last_updated": data["last_updated"]})


def _format_date_with_weekday(date_str):
    """加上星期。'2026-06-05' → '2026-06-05(四)'。已有 (X) 結尾的保留原樣。"""
    if not date_str:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
    if not m:
        return date_str
    ymd = m.group(1)
    if re.search(r"\([一二三四五六日]\)$", date_str):
        return date_str
    try:
        dt = datetime.strptime(ymd, "%Y-%m-%d")
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return f"{ymd}({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def _format_fee(fee):
    """費用顯示:免費類 → 「免費」;空/0 → 「—」;數字 → 「XXX 元」"""
    s = str(fee or "").strip()
    if not s or s == "0":
        return "—"
    if "免" in s:
        return "免費"
    if s.endswith("元"):
        return s
    return f"{s} 元"


def _format_date_range(course):
    """組合 start_date / end_date → 「2026-06-01(一) 至 2026-06-05(五)」;同一天只顯示開始日。"""
    start = _format_date_with_weekday(course.get("start_date", ""))
    end = _format_date_with_weekday(course.get("end_date", ""))
    if not start:
        return ""
    if not end:
        return start
    s_pure = re.sub(r"\([^)]*\)$", "", start)
    e_pure = re.sub(r"\([^)]*\)$", "", end)
    if s_pure == e_pure:
        return start
    return f"{start} 至 {end}"


@app.route("/api/email", methods=["POST"])
@login_required

def api_email():
    body = request.get_json() or {}
    # 新版:多同仁不同部門
    employees = body.get("employees", [])

    # 向下相容:如果還有人送舊格式 (emp_name + emp_dept) 也能用
    if not employees:
        emp_name = body.get("emp_name", "")
        emp_dept = body.get("emp_dept", "")
        name_list = [n.strip() for n in re.split(r"[,，、\s]+", emp_name) if n.strip()]
        employees = [{"name": n, "dept": emp_dept} for n in name_list]

    # 過濾掉空白名字,若完全沒人就放預設
    employees = [e for e in employees if e.get("name", "").strip()]
    if not employees:
        employees = [{"name": "(同仁姓名)", "dept": "(部門)"}]

    emp_month = body.get("emp_month", "")
    course_name = body.get("course_name", "")
    train_type = body.get("train_type", "複訓")
    selected = body.get("selected_courses", [])
    mode = body.get("mode", "external")  # "external" 給同仁 / "internal" 給後台

    name_display = "、".join(e["name"] for e in employees)
    name_suffix = f"_{name_display}" if name_display and name_display != "(同仁姓名)" else ""
    subject = f"2026/{emp_month} 外訓通知(請回覆可以安排的時段)_{course_name}{name_suffix}"

 # 優先用手動輸入的「欲派訓課程名稱」當關鍵字,沒填才退而求其次用勾選的課程
    ref_name = course_name if course_name else (selected[0]["name"] if selected else "")
    ref_hours = selected[0]["hours"] if selected else "3"
    outline = get_course_outline(ref_name, ref_hours, train_type)

    # 每位同仁一列,各自部門
    emp_rows = "".join(
        f'<tr><td style="border:1px solid #BBB;">{course_name}</td>'
        f'<td style="border:1px solid #BBB;">{e["dept"] or "(未填)"}</td>'
        f'<td style="border:1px solid #BBB;">{e["name"]}</td>'
        f'<td style="border:1px solid #BBB;">{emp_month} 月</td></tr>'
        for e in employees
    )
    
    # 共用開頭
    html_parts = [
        '<div style="font-family:微軟正黑體,sans-serif;font-size:14px;color:#333;line-height:1.7;">',
        '<p>Dear All,</p>',
        f'<p>訓練單位將安排以下同仁外出訓練進行職安證照<b>{train_type}</b>課程,派訓人員名單如下:</p>',
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #888;margin:10px 0;">',
        '<tr style="background:#4472C4;color:white;font-weight:bold;">',
        '<td style="border:1px solid #BBB;">課程名稱</td><td style="border:1px solid #BBB;">部門</td>',
        '<td style="border:1px solid #BBB;">姓名</td><td style="border:1px solid #BBB;">預訂月份</td></tr>',
        emp_rows,
        '</table>',
        '<p><b>📋 課程介紹:</b></p>',
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #888;margin:10px 0;width:100%;">',
        f'<tr><td style="background:#E7E6E6;width:100px;border:1px solid #BBB;"><b>法規依據</b></td><td style="border:1px solid #BBB;">{outline["law"].replace(chr(10), "<br>")}</td></tr>',
        f'<tr><td style="background:#E7E6E6;border:1px solid #BBB;"><b>訓練目的</b></td><td style="border:1px solid #BBB;">{outline["purpose"]}</td></tr>',
        f'<tr><td style="background:#E7E6E6;vertical-align:top;border:1px solid #BBB;"><b>課程大綱</b></td><td style="border:1px solid #BBB;">{"<br>".join(outline["outline"])}</td></tr>',
        '</table>',
        '<p>再請回覆可以安排受訓的日期及時段,若以下時段不合適,再請聯繫我們,謝謝!</p>',
        '<p><b>相關課程時段如下:</b></p>',
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #888;margin:10px 0;">',
    ]
    
    # 合併版:不分同仁/後台,一張完整表;按開課日升冪排序(近的在前)
    selected_sorted = sorted(selected, key=lambda c: c.get("start_date", "") or "9999-99-99")
    html_parts.extend([
        '<tr style="background:#FFF3CD;"><td colspan="8" style="border:1px solid #BBB;color:#C44569;font-weight:700;padding:10px;text-align:center;font-size:13px;">※ 報名連結為訓練單位協助報名使用,請勿自行點擊/報名</td></tr>',
        '<tr style="background:#4472C4;color:white;font-weight:bold;">',
        '<td style="border:1px solid #BBB;">場次</td>',
        '<td style="border:1px solid #BBB;">主辦單位</td>',
        '<td style="border:1px solid #BBB;">日期</td>',
        '<td style="border:1px solid #BBB;">上課時間</td>',
        '<td style="border:1px solid #BBB;">上課地點</td>',
        '<td style="border:1px solid #BBB;">時數</td>',
        '<td style="border:1px solid #BBB;">費用</td>',
        '<td style="border:1px solid #BBB;">報名連結</td></tr>',
    ])
    for i, c in enumerate(selected_sorted, 1):
        reg_link = c.get("register_url", "")
        link_html = f'<a href="{reg_link}" target="_blank">點此報名</a>' if reg_link else "(無連結)"
        html_parts.append(
            f'<tr><td style="text-align:center;border:1px solid #BBB;"><b>{i}</b></td>'
            f'<td style="border:1px solid #BBB;">{c.get("institute","")} ({c.get("branch","")})</td>'
            f'<td style="border:1px solid #BBB;">{_format_date_range(c)}</td>'
            f'<td style="border:1px solid #BBB;">{c.get("class_time","")}</td>'
            f'<td style="border:1px solid #BBB;">{c.get("location","").replace(chr(10), "<br>")}</td>'
            f'<td style="text-align:center;border:1px solid #BBB;">{c.get("hours","")} 小時</td>'
            f'<td style="text-align:right;border:1px solid #BBB;">{_format_fee(c.get("fee",""))}</td>'
            f'<td style="border:1px solid #BBB;">{link_html}</td></tr>'
        )
    
    html_parts.extend([
        '</table>',
        '<p>敬請回覆,謝謝!</p>',
        '</div>',
    ])
    return jsonify({"subject": subject, "html": "".join(html_parts)})


# ==========================================================================
# 登入頁
# ==========================================================================
LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><title>登入 - External Training Management System</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Microsoft JhengHei', sans-serif;
  background: linear-gradient(135deg, #A8D8EA 0%, #AA96DA 50%, #B5EAD7 100%);
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
}
.login-box {
  background: white; border-radius: 20px; padding: 44px;
  box-shadow: 0 20px 60px rgba(100,120,150,0.25); width: 420px;
}
.login-box .brand { color: #3F72AF; font-size: 14px; font-weight: 700; letter-spacing: 1px; margin-bottom: 4px;}
.login-box h1 { color: #3F72AF; font-size: 22px; margin-bottom: 8px; }
.login-box .sub { color: #888; font-size: 13px; margin-bottom: 32px; }
.field { margin-bottom: 18px; }
.field label { display: block; font-size: 13px; color: #555; margin-bottom: 8px; font-weight: 600; }
.field input {
  width: 100%; padding: 13px 16px; border: 2px solid #DBE9EE;
  border-radius: 10px; font-size: 15px; font-family: inherit; background: #F8FBFD;
}
.field input:focus { outline: none; border-color: #87BDD8; background: white; }
button {
  width: 100%; background: linear-gradient(135deg, #87BDD8, #B5EAD7); color: #3F4E5C; border: none;
  padding: 14px; font-size: 16px; font-weight: 700; border-radius: 10px;
  cursor: pointer; font-family: inherit; margin-top: 12px;
  box-shadow: 0 4px 12px rgba(135,189,216,0.4);
  transition: transform 0.1s;
}
button:hover { transform: translateY(-2px); }
button.force-btn { 
  background: linear-gradient(135deg, #FFB6B9, #FAE3D9); color: #B73E3E; margin-top: 8px;
  box-shadow: 0 4px 12px rgba(255,182,185,0.4);
}
.error { background: #FFE5E5; color: #C44569; padding: 12px; border-radius: 8px; margin-bottom: 16px; font-size: 13px; line-height: 1.5; }
.footer { text-align: center; margin-top: 22px; font-size: 11px; color: #AAA; line-height: 1.6; }
</style></head>
<body>
<div class="login-box">
  <div class="brand">EXTERNAL TRAINING MANAGEMENT</div>
  <h1>📚 外訓課程整合管理系統</h1>
  <div class="sub">請輸入您的帳號及密碼</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post">
    <div class="field">
      <label>帳號</label>
      <input type="text" name="username" required autofocus value="{{ _username or '' }}">
    </div>
    <div class="field">
      <label>密碼</label>
      <input type="password" name="password" required value="{{ _password or '' }}">
    </div>
    <button type="submit">登入</button>
    {% if show_force %}
    <input type="hidden" name="force" value="1">
    <button type="submit" class="force-btn" onclick="return confirm('確定要強制登入嗎? 對方會被踢出系統')">⚡ 強制登入 (踢掉舊使用者)</button>
    {% endif %}
  </form>
  <div class="footer">
    External Training Management System v7.0<br>
    © 2026 Training Dept. ｜ System internal use only.
  </div>
</div>
</body></html>
"""


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>外訓課程整合工具</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --sky: #A8D8EA;       /* 淡天藍 */
    --mint: #B5EAD7;      /* 淡薄荷 */
    --lavender: #DCD3FF;  /* 淡薰衣草 */
    --blue: #3F72AF;      /* 主藍 */
    --teal: #4FB3BF;      /* 主青 */
    --warm: #FFF4E6;      /* 暖背景 */
    --paper: #FAFCFE;
    --ink: #2C3E50;
    --ink-soft: #5B7080;
    --line: #E5EEF4;
  }
  body { font-family: 'Microsoft JhengHei', sans-serif; background: var(--paper); color: var(--ink); min-height: 100vh; }
  
  /* === Header === */
  .header {
    background: linear-gradient(135deg, var(--sky) 0%, var(--mint) 100%);
    color: var(--ink); padding: 14px 28px;
    display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 2px 12px rgba(100,160,200,0.15);
    position: sticky; top: 0; z-index: 100;
  }
  .header-left h1 { font-size: 20px; font-weight: 700; }
  .header-left .sub { font-size: 12px; opacity: 0.75; margin-top: 2px; }
  .header-right { display: flex; align-items: center; gap: 14px; }
  .header-stat {
    background: rgba(255,255,255,0.6); padding: 6px 12px; border-radius: 8px;
    font-size: 12px; color: var(--ink);
    display: flex; align-items: center; gap: 6px;
  }
  .header-stat strong { color: var(--blue); font-weight: 700; }
  .user-info { font-size: 13px; background: rgba(255,255,255,0.6); padding: 6px 12px; border-radius: 8px; }
  .user-info .name { font-weight: 700; color: var(--blue); }
  .user-info .role { background: var(--blue); color: white; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
  .logout-btn {
    background: white; border: none; color: var(--blue); padding: 8px 16px;
    font-family: inherit; font-size: 13px; border-radius: 8px; cursor: pointer;
    font-weight: 600; transition: all 0.15s;
  }
  .logout-btn:hover { background: var(--lavender); }
  
  .container { max-width: 1400px; margin: 0 auto; padding: 20px 40px; }
  
  /* === Cards === */
  .card {
    background: white; border-radius: 14px; padding: 22px; margin-bottom: 16px;
    box-shadow: 0 2px 8px rgba(160,180,200,0.12);
  }
  .card h2 { font-size: 16px; margin-bottom: 16px; color: var(--blue); display: flex; align-items: center; gap: 10px; font-weight: 700; }
  .card h2::before { content:''; width:5px; height:20px; background: var(--teal); border-radius: 3px; }
  
  /* === Buttons === */
  button.btn-primary {
    background: linear-gradient(135deg, var(--blue), var(--teal));
    color: white; border: none; padding: 12px 24px;
    font-family: inherit; font-size: 14px; font-weight: 700;
    border-radius: 10px; cursor: pointer; transition: all 0.15s;
    box-shadow: 0 3px 10px rgba(63,114,175,0.3);
  }
  button.btn-primary:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(63,114,175,0.4); }
  button.btn-secondary {
    background: white; color: var(--blue); border: 2px solid var(--sky);
    padding: 10px 20px; font-family: inherit; font-size: 13px; font-weight: 700;
    border-radius: 10px; cursor: pointer; transition: all 0.15s;
  }
  button.btn-secondary:hover { background: var(--sky); }
  
  /* === Institute update button (commit 15: 單獨更新此協會) === */
  .inst-update-btn {
    background: white; color: var(--blue); border: 1.5px solid var(--blue);
    padding: 6px 12px; font-family: inherit; font-size: 11px; font-weight: 700;
    border-radius: 8px; cursor: pointer; flex-shrink: 0; white-space: nowrap;
    transition: all 0.15s;
  }
  .inst-update-btn:hover {
    background: var(--blue); color: white; transform: translateY(-1px);
  }

  /* === Institute card (協會勾選) === */
  .inst-card {
    background: linear-gradient(135deg, #F0F8FF 0%, #E8F5F0 100%);
    border: 2.5px solid var(--sky);
    border-radius: 12px; padding: 14px 20px;
    display: flex; align-items: center; gap: 12px;
    cursor: pointer; transition: all 0.15s;
    user-select: none;
  }
  .inst-card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(168,216,234,0.4); }
  .inst-card.checked {
    background: linear-gradient(135deg, var(--mint) 0%, var(--sky) 100%);
    border-color: var(--blue);
  }
  .inst-card input { width: 22px; height: 22px; cursor: pointer; accent-color: var(--blue); }
  .inst-card .label { font-weight: 600; font-size: 14px; }
  .inst-card .desc { font-size: 11px; color: var(--ink-soft); }
  
  /* === Inputs === */
  input[type=text], input[type=number], select {
    padding: 10px 14px; border: 2px solid var(--line); border-radius: 10px;
    font-family: inherit; font-size: 14px; background: var(--paper);
    transition: border-color 0.15s;
  }
  input:focus, select:focus { outline: none; border-color: var(--teal); background: white; }
  label { font-size: 13px; color: var(--ink-soft); font-weight: 600; }
  
  /* === Badges === */
  .badge { display: inline-block; padding: 4px 11px; border-radius: 14px; font-size: 11px; font-weight: 700; }
  .badge-open { background: #D4EDDA; color: #1B5E20; }
  .badge-full { background: #FFE5E5; color: #B71C1C; }
  .badge-promo { background: #FFF3CD; color: #856404; }
  .badge-other { background: #E2E3E5; color: #383D41; }
  .badge-cat-init { background: #CCE5FF; color: #004085; }
  .badge-cat-re { background: #F3E0FF; color: #4A148C; }
  .badge-nat-tw { background: #E8F5E9; color: #1B5E20; }
  .badge-nat-fg { background: #FFF8E1; color: #6D4C41; }
  
  /* === Table === */
  .filter-bar {
    display: grid;
    grid-template-columns: 2fr 1fr 1fr 1fr 1fr 1fr 1fr;
    gap: 10px; margin-bottom: 14px;
  }
  .stat { font-size: 13px; color: var(--ink-soft); margin-bottom: 10px; }
  .stat strong { color: var(--teal); font-weight: 700; font-size: 18px; }
  
  .table-wrap {
    max-height: 480px; overflow-y: auto;
    border: 2px solid var(--line); border-radius: 12px; background: white;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  
  /* === Sticky header (第 4 點) === */
  thead { position: sticky; top: 0; z-index: 10; }
  thead tr { background: var(--blue) !important; }
  th {
    padding: 12px 8px; text-align: left; font-weight: 700;
    color: white; background: var(--blue);
    border-bottom: 2px solid var(--teal);
  }
  td { padding: 10px 8px; border-bottom: 1px solid var(--line); }
  
  /* === Row selection (第 2 點 - 整列點擊) === */
  tbody tr { cursor: pointer; transition: background 0.1s; }
  tbody tr:hover { background: #F0F8FF; }
  tbody tr.selected { background: linear-gradient(90deg, #FFF4E6, #FFE9C7); }
  tbody tr.selected:hover { background: linear-gradient(90deg, #FFE9C7, #FFE0A8); }
  
  /* 大型勾選方塊 */
  .big-check {
    width: 24px; height: 24px; border: 2.5px solid var(--ink-soft);
    border-radius: 6px; display: flex; align-items: center; justify-content: center;
    background: white; font-size: 18px; color: var(--blue);
  }
  tr.selected .big-check {
    background: var(--blue); border-color: var(--blue); color: white;
  }
  
  /* === Selected list (第 3 點 - 取消按鈕優化) === */
  .selected-list {
    background: linear-gradient(135deg, #FFF9E6 0%, #FFF4E6 100%);
    border: 2.5px dashed #F4B860;
    border-radius: 12px; padding: 14px; margin-bottom: 14px; min-height: 70px;
  }
  .sel-item {
    background: white; padding: 12px 14px; border-radius: 10px;
    margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  .sel-item .info { flex: 1; font-size: 13px; }
  .sel-item .info b { color: var(--blue); }
  .sel-item .info .meta { font-size: 11px; color: var(--ink-soft); margin-top: 3px; }
  .sel-item .remove-btn {
    background: #FFE5E5; color: #C44569; border: 2px solid #FFB8B8;
    padding: 8px 16px; border-radius: 8px; cursor: pointer;
    font-family: inherit; font-size: 13px; font-weight: 700;
    transition: all 0.15s;
  }
  .sel-item .remove-btn:hover { background: #C44569; color: white; border-color: #C44569; }
  
  /* === Forms === */
  .form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-bottom: 16px; }
  .form-grid label { display: block; margin-bottom: 6px; }
  .form-grid input, .form-grid select { width: 100%; }
  
  /* === Loading & Toast === */
  .loading {
    display: none; background: rgba(0,0,0,0.5); position: fixed;
    top: 0; left: 0; right: 0; bottom: 0; z-index: 999;
    align-items: center; justify-content: center;
  }
  .loading.show { display: flex; }
  .loading-box { background: white; padding: 32px 48px; border-radius: 14px; text-align: center; }
  .spinner {
    border: 4px solid var(--line); border-top: 4px solid var(--teal);
    border-radius: 50%; width: 44px; height: 44px;
    animation: spin 1s linear infinite; margin: 0 auto 14px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  
  .toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--blue); color: white; padding: 14px 22px;
    border-radius: 10px; box-shadow: 0 6px 16px rgba(0,0,0,0.2);
    z-index: 1000; opacity: 0; transition: opacity 0.3s; font-weight: 600;
  }
  .toast.show { opacity: 1; }
  .toast.success { background: #4FB3BF; }
  .toast.error { background: #C44569; }
  
  /* === Modal === */
  .modal {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5); z-index: 998;
    align-items: center; justify-content: center; padding: 24px;
  }
  .modal.show { display: flex; }
  .modal-box {
    background: white; border-radius: 14px; max-width: 1000px; width: 100%;
    max-height: 90vh; overflow: hidden; display: flex; flex-direction: column;
  }
  .modal-head {
    padding: 16px 22px; border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
  }
  .modal-meta {
    padding: 10px 22px; background: var(--paper);
    border-bottom: 1px solid var(--line); font-size: 13px;
  }
  .modal-body { padding: 22px; overflow-y: auto; flex: 1; }
  
  /* === Auto update panel === */
  .setting-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 0; border-bottom: 1px dashed var(--line);
  }
  .setting-row:last-child { border-bottom: none; }
  .toggle-switch {
    width: 44px; height: 24px; background: #ccc; border-radius: 12px;
    position: relative; cursor: pointer; transition: background 0.2s;
  }
  .toggle-switch.on { background: var(--teal); }
  .toggle-switch::after {
    content: ''; position: absolute; width: 20px; height: 20px;
    background: white; border-radius: 50%; top: 2px; left: 2px;
    transition: left 0.2s;
  }
  .toggle-switch.on::after { left: 22px; }
  
  details { margin-top: 12px; }
  details summary {
    cursor: pointer; color: var(--blue); font-size: 13px; font-weight: 600;
    padding: 6px 0;
  }
  
  .info-line { font-size: 12px; color: var(--ink-soft); margin-top: 6px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div style="font-size:10px;letter-spacing:1.5px;font-weight:700;opacity:0.7;">EXTERNAL TRAINING MANAGEMENT</div>
    <h1>📚 外訓課程整合管理系統</h1>
    <div class="sub">自動抓取協會課程資料 → 視覺化挑選 → 一鍵產生派訓信件</div>
  </div>
  <div class="header-right">
    <div class="header-stat">
      ⏱ 使用時間 <strong id="usageTime">0</strong> 分鐘
    </div>
    <div class="header-stat">
      👥 在線 <strong id="onlineCount">1</strong> 人
    </div>
    <div class="user-info">
      <span class="name">{{ user.display_name }}</span>
      <span class="role">{{ '管理員' if user.role == 'admin' else '使用者' }}</span>
    </div>
    <button class="logout-btn" onclick="logout()">登出</button>
  </div>
</div>

<div class="container">

  <!-- ① 抓取課程 -->
  <div class="card">
    <h2>① 抓取課程資料</h2>
    <div style="display:flex; gap:14px; align-items:center; flex-wrap:wrap;">
      <label class="inst-card checked" id="instTicsha" onclick="toggleInst('ticsha')">
        <input type="checkbox" id="instCheckTicsha" checked>
        <div style="flex:1;">
          <div class="label">台灣省工商安全衛生協會</div>
          <div class="desc">中壢 · 桃園 · 新竹 三個分會</div>
        </div>
        <button type="button" class="inst-update-btn" onclick="event.stopPropagation();event.preventDefault();updateOnly('ticsha');" title="只更新此協會,不影響其他">🔄 只更新</button>
      </label>
      <label class="inst-card checked" id="instCpc" onclick="toggleInst('cpc')">
        <input type="checkbox" id="instCheckCpc" checked>
        <div style="flex:1;">
          <div class="label">中國生產力中心 (CPC)</div>
          <div class="desc">桃園 · 台北承德 / 職安·消防·營建 三類</div>
        </div>
        <button type="button" class="inst-update-btn" onclick="event.stopPropagation();event.preventDefault();updateOnly('cpc');" title="只更新此協會,不影響其他">🔄 只更新</button>
      </label>
      <label class="inst-card checked" id="instIsha" onclick="toggleInst('isha')">
        <input type="checkbox" id="instCheckIsha" checked>
        <div style="flex:1;">
          <div class="label">中華民國工業安全衛生協會 (ISHA)</div>
          <div class="desc">北區 5 個職訓中心:台北·新北·桃園·中壢·新竹</div>
        </div>
        <button type="button" class="inst-update-btn" onclick="event.stopPropagation();event.preventDefault();updateOnly('isha');" title="只更新此協會,不影響其他">🔄 只更新</button>
      </label>
      <button class="btn-primary" onclick="startUpdate()">🔄 立即更新</button>
    </div>
    <div class="info-line" id="lastUpdate">尚未抓取</div>
    
    <details>
      <summary>⚙️ 自動更新設定 (進階)</summary>
      <div style="margin-top:10px; padding:12px; background:var(--paper); border-radius:10px;">
        <div class="setting-row">
          <div>
            <div style="font-weight:600;">啟用自動更新</div>
            <div class="info-line">每天指定時間自動抓取所有協會的課程資料</div>
          </div>
          <div class="toggle-switch" id="autoUpdateToggle" onclick="alert('自動更新功能將於下版開放\\n屆時可設定每天 / 每週的自動抓取時間')"></div>
        </div>
        <div class="setting-row">
          <div>
            <div style="font-weight:600;">⚡ 強制全抓 (忽略 cache)</div>
            <div class="info-line">懷疑協會偷改地址、或第一次跑時用。會比「立即更新」慢一些,平常不需要按。</div>
          </div>
          <button type="button" onclick="if(confirm('確定要強制全抓嗎?\n\n所有協會的詳細頁都會重抓,時間約 8-10 分鐘。\n平常請用上方「立即更新」即可。')) startUpdate(true);"
                  style="background:linear-gradient(135deg,#FFB6B9,#FAE3D9);color:#B73E3E;border:none;padding:10px 18px;border-radius:10px;font-family:inherit;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;flex-shrink:0;">
            ⚡ 強制全抓
          </button>
        </div>
      </div>
    </details>
  </div>

  <!-- ② 篩選並挑選 -->
  <div class="card">
    <h2>② 篩選並挑選課程</h2>
    <div class="filter-bar">
      <input type="text" id="searchKw" placeholder="🔍 搜尋課程名稱 (例: 粉塵, 堆高機)" oninput="renderTable()">
      <select id="filterInstitute" onchange="renderTable()">
        <option value="">全部主辦單位</option>
        <option value="台灣省工商安全衛生協會">台灣省工商安全衛生協會</option>
        <option value="中國生產力中心">中國生產力中心 (CPC)</option>
        <option value="中華民國工業安全衛生協會">中華民國工業安全衛生協會 (ISHA)</option>
      </select>
      <select id="filterBranch" onchange="renderTable()" style="display:none;"><option value="">全部分會</option></select>
      <div id="branchMultiBox" style="position:relative;">
        <button type="button" onclick="toggleBranchDropdown()" id="branchToggleBtn"
                style="width:100%;padding:10px 14px;border:2px solid var(--line);border-radius:10px;background:var(--paper);text-align:left;cursor:pointer;font-family:inherit;font-size:14px;">
          📍 全部分會
        </button>
        <div id="branchDropdown" style="display:none;position:absolute;top:100%;left:0;right:0;background:white;border:2px solid var(--teal);border-radius:10px;margin-top:4px;padding:10px;z-index:50;box-shadow:0 4px 12px rgba(0,0,0,0.15);max-height:240px;overflow-y:auto;">
          <div id="branchOptions"></div>
        </div>
      </div>
      <select id="filterCategory" onchange="renderTable()">
        <option value="複訓" selected>複訓 (預設)</option>
        <option value="初訓">初訓</option>
        <option value="">全部類別</option>
      </select>
      <select id="filterNationality" onchange="renderTable()">
        <option value="本國籍" selected>只要本國籍 (預設)</option>
        <option value="">全部 (含外籍)</option>
        <option value="越南籍">只要越南籍</option>
        <option value="印尼籍">只要印尼籍</option>
        <option value="菲律賓籍">只要菲律賓籍</option>
        <option value="泰國籍">只要泰國籍</option>
      </select>
      <select id="filterStatus" onchange="renderTable()">
        <option value="">全部狀態</option>
        <option value="open">確定開班/招生中</option>
        <option value="full">額滿</option>
      </select>
      <select id="filterClass" onchange="renderTable()">
        <option value="">全部班別</option>
        <option value="日">日間班</option>
        <option value="夜">夜間班</option>
        <option value="假">假日班</option>
      </select>
    </div>
    <div class="stat">
      共 <strong id="totalCount">0</strong> 筆,目前顯示 <strong id="visCount">0</strong> 筆,已選擇 <strong id="selCount">0</strong> 筆
      <span style="float:right;color:var(--ink-soft);font-size:12px;">💡 點選整列即可勾選/取消</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th width="50">選</th><th>課程名稱</th><th width="70">分會</th>
          <th width="60">類別</th><th width="65">國籍</th>
          <th width="110">開課日</th><th width="85">班別</th>
          <th width="50">時數</th><th width="60">費用</th><th width="90">狀態</th>
        </tr></thead>
        <tbody id="tbody">
          <tr><td colspan="10" style="text-align:center;padding:50px;color:#999;">請先按「立即更新」抓取資料</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ③ 產生派訓信件 -->
  <div class="card">
    <h2>③ 產生派訓信件 (含課程大綱)</h2>
    <div class="selected-list" id="selectedList">
      <div style="color:#999;text-align:center;padding:10px;">尚未挑選任何課程</div>
    </div>
    <div class="form-grid">
  <div style="grid-column:1/-1;">
          <label>派訓同仁(可加多人,每人各自部門)</label>
          <div id="empList" style="display:flex;flex-direction:column;gap:6px;margin-top:4px;"></div>
          <button type="button" onclick="addEmployee()" style="margin-top:8px;background:#E7F3FF;border:2px dashed #3F72AF;color:#3F72AF;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;">➕ 新增同仁</button>
        </div>
        
      <div><label>預訂月份</label><input type="number" id="empMonth" placeholder="例: 5" min="1" max="12"></div>
      <div>
        <label>參訓類別</label>
        <select id="trainType"><option>複訓</option><option>初訓</option><option>在職教育訓練</option></select>
      </div>
      <div style="grid-column:1/3;">
        <label>欲派訓課程名稱</label>
        <input type="text" id="courseName" placeholder="例: 粉塵作業主管-複訓">
      </div>
    </div>
    
    <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:8px;">
      <button class="btn-primary" onclick="generateExternal()">📧 複製信件 (Outlook 可直接貼)</button>
      <button class="btn-secondary" onclick="previewExternal()">👁️ 預覽信件</button>
    </div>
    <div class="info-line">
      信件含完整資訊:場次、主辦、日期(含星期)、時間、地點、班別、時數、費用、狀態、報名連結 — 按開課日升冪排序
    </div>
  </div>

  <div style="text-align:center;padding:20px;color:#999;font-size:11px;line-height:1.8;">
    External Training Management System v7.0<br>
    © 2026 Training Dept. ｜ System internal use only.
  </div>

</div>

<div class="loading" id="loading"><div class="loading-box"><div class="spinner"></div><div id="loadingText">處理中...</div></div></div>
<div class="toast" id="toast"></div>

<!-- 預覽 Modal -->
<div class="modal" id="previewModal">
  <div class="modal-box">
    <div class="modal-head">
      <strong>📧 信件預覽 <span id="prevMode" style="background:var(--lavender);padding:2px 10px;border-radius:10px;font-size:12px;margin-left:8px;"></span></strong>
      <button class="btn-secondary" onclick="document.getElementById('previewModal').classList.remove('show')">關閉</button>
    </div>
    <div class="modal-meta"><strong>主旨:</strong> <span id="prevSubject"></span></div>
    <div class="modal-body" id="prevBody"></div>
  </div>
</div>

<script>
let allCourses = [];
let selected = new Map();
let startTime = Date.now();

// 使用時間計時器
setInterval(() => {
  const mins = Math.floor((Date.now() - startTime) / 60000);
  document.getElementById('usageTime').textContent = mins;
}, 5000);

// 在線人數刷新
async function refreshOnline() {
  try {
    const resp = await fetch('/api/online');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    document.getElementById('onlineCount').textContent = data.count;
  } catch (e) {}
}
setInterval(refreshOnline, 30000);

function showLoading(t) { document.getElementById('loadingText').textContent = t || '處理中...'; document.getElementById('loading').classList.add('show'); }
function hideLoading() { document.getElementById('loading').classList.remove('show'); }
function toast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + (type || '');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function logout() {
  if (confirm('確定要登出嗎?')) window.location.href = '/logout';
}

function toggleInst(code) {
  const cb = document.getElementById('instCheck' + code.charAt(0).toUpperCase() + code.slice(1));
  cb.checked = !cb.checked;
  const card = document.getElementById('inst' + code.charAt(0).toUpperCase() + code.slice(1));
  card.classList.toggle('checked', cb.checked);
}

async function loadCourses() {
  try {
    const resp = await fetch('/api/courses');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    allCourses = data.courses || [];
    document.getElementById('lastUpdate').textContent = data.last_updated ? '上次更新: ' + data.last_updated : '尚未抓取';
    populateBranches();
    renderTable();
  } catch (e) { console.error(e); }
}

let selectedBranches = new Set();  // 已選的分會

function populateBranches() {
  const branches = [...new Set(allCourses.map(c => c.branch))].sort();
  // 預設全選
  if (selectedBranches.size === 0) {
    branches.forEach(b => selectedBranches.add(b));
  }
  const opts = branches.map(b => `
    <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" 
           onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''">
      <input type="checkbox" value="${b}" ${selectedBranches.has(b)?'checked':''} 
             onchange="onBranchCheck(this)" style="width:18px;height:18px;cursor:pointer;">
      <span style="font-size:14px;">${b}</span>
    </label>
  `).join('');
  document.getElementById('branchOptions').innerHTML = `
    <div style="border-bottom:1px solid #eee;padding-bottom:6px;margin-bottom:6px;display:flex;gap:6px;">
      <button onclick="selectAllBranches()" style="flex:1;background:var(--teal);color:white;border:none;padding:5px;border-radius:5px;cursor:pointer;font-size:12px;">全選</button>
      <button onclick="clearAllBranches()" style="flex:1;background:#ccc;color:white;border:none;padding:5px;border-radius:5px;cursor:pointer;font-size:12px;">清除</button>
    </div>
    ${opts}
  `;
  updateBranchButtonText();
}

function toggleBranchDropdown() {
  const d = document.getElementById('branchDropdown');
  d.style.display = d.style.display === 'none' ? 'block' : 'none';
}

function onBranchCheck(cb) {
  if (cb.checked) selectedBranches.add(cb.value);
  else selectedBranches.delete(cb.value);
  updateBranchButtonText();
  renderTable();
}

function selectAllBranches() {
  document.querySelectorAll('#branchOptions input[type=checkbox]').forEach(cb => {
    cb.checked = true;
    selectedBranches.add(cb.value);
  });
  updateBranchButtonText();
  renderTable();
}

function clearAllBranches() {
  document.querySelectorAll('#branchOptions input[type=checkbox]').forEach(cb => {
    cb.checked = false;
  });
  selectedBranches.clear();
  updateBranchButtonText();
  renderTable();
}

function updateBranchButtonText() {
  const all = [...new Set(allCourses.map(c => c.branch))];
  const sel = [...selectedBranches];
  const btn = document.getElementById('branchToggleBtn');
  if (sel.length === 0) btn.textContent = '📍 (未選分會)';
  else if (sel.length === all.length) btn.textContent = `📍 全部分會 (${all.length})`;
  else btn.textContent = `📍 ${sel.join(', ')}`;
}

// 點外面關閉下拉
document.addEventListener('click', function(e) {
  const box = document.getElementById('branchMultiBox');
  if (box && !box.contains(e.target)) {
    document.getElementById('branchDropdown').style.display = 'none';
  }
});

// === Commit 15: 單獨更新某一個協會 (不影響其他) ===
async function updateOnly(code) {
  const labels = {ticsha: '台灣省工商安全衛生協會', cpc: '中國生產力中心', isha: '中華民國工業安全衛生協會'};
  const label = labels[code] || code;
  if (!confirm(`只更新「${label}」?\n\n其他協會的資料會保留不動。`)) return;
  showLoading(`正在抓取 ${label}...`);
  const progressTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/update_progress');
      const p = await r.json();
      if (p.stage === 'list') {
        document.getElementById('loadingText').innerHTML =
          `📋 ${p.message}<br><small style="opacity:0.7;">${p.current}/${p.total}</small>`;
      } else if (p.stage === 'details') {
        const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
        document.getElementById('loadingText').innerHTML =
          `📍 ${p.message}<br>
           <small style="opacity:0.7;">${p.current}/${p.total} (${pct}%)</small><br>
           <div style="background:#eee;border-radius:8px;height:8px;margin-top:8px;overflow:hidden;width:280px;">
             <div style="background:linear-gradient(90deg,#4FB3BF,#87BDD8);height:100%;width:${pct}%;transition:width 0.3s;"></div>
           </div>`;
      }
    } catch(e) {}
  }, 1000);
  try {
    const resp = await fetch('/api/update', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scrapers: [code]})
    });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (data.ok) {
      toast(`✓ ${label} 更新完成! 共 ${data.count} 筆課程`, 'success');
      await loadCourses();
    }
  } catch (e) {
    toast('更新失敗: ' + e.message, 'error');
  } finally {
    clearInterval(progressTimer);
    hideLoading();
  }
}

async function startUpdate(forceRefresh = false) {
  showLoading(forceRefresh ? '⚡ 強制全抓中(較慢,請耐心等候)...' : '正在抓取課程資料...');
  
  // 啟動 polling 取得進度
  const progressTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/update_progress');
      const p = await r.json();
      if (p.stage === 'list') {
        document.getElementById('loadingText').innerHTML = 
          `📋 ${p.message}<br><small style="opacity:0.7;">第 ${p.current}/${p.total} 個分會</small>`;
      } else if (p.stage === 'details') {
        const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
        document.getElementById('loadingText').innerHTML = 
          `📍 抓取詳細上課地點中...<br>
           <small style="opacity:0.7;">${p.current}/${p.total} (${pct}%)</small><br>
           <div style="background:#eee;border-radius:8px;height:8px;margin-top:8px;overflow:hidden;width:280px;">
             <div style="background:linear-gradient(90deg,#4FB3BF,#87BDD8);height:100%;width:${pct}%;transition:width 0.3s;"></div>
           </div>
           <small style="opacity:0.6;font-size:11px;margin-top:6px;display:block;">${forceRefresh ? '強制全抓:約 8-10 分鐘' : '一般更新:約 3-5 分鐘(cache 生效後)'},請耐心等候</small>`;
      }
    } catch(e) {}
  }, 1000);
  
  try {
// 收集所有勾選的協會
    const scraperCodes = [];
    if (document.getElementById('instTicsha').classList.contains('checked')) scraperCodes.push('ticsha');
    if (document.getElementById('instCpc').classList.contains('checked')) scraperCodes.push('cpc');
    if (document.getElementById('instIsha').classList.contains('checked')) scraperCodes.push('isha');
    if (scraperCodes.length === 0) {
      toast('請至少勾選一個協會', 'error');
      clearInterval(progressTimer);
      hideLoading();
      return;
    }
    const resp = await fetch('/api/update', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scrapers: scraperCodes, force_refresh: forceRefresh})
    });
    
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (data.ok) {
      toast(`✓ 更新完成! 共 ${data.count} 筆課程${forceRefresh ? ' (強制全抓)' : ''}`, 'success');
      await loadCourses();
    }
  } catch (e) {
    toast('更新失敗: ' + e.message, 'error');
  } finally {
    clearInterval(progressTimer);
    hideLoading();
  }
}

// === 日期/時數 格式化 helpers (commit 14) ===
function _formatDateWithWeekday(s) {
  if (!s) return '';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return s;
  if (/\([一二三四五六日]\)$/.test(s)) return s;  // 已有星期就保留原樣
  const [, yy, mm, dd] = m;
  const dt = new Date(parseInt(yy), parseInt(mm)-1, parseInt(dd));
  if (isNaN(dt.getTime())) return s;
  return `${yy}-${mm}-${dd}(${['日','一','二','三','四','五','六'][dt.getDay()]})`;
}
function _formatDateRange(c) {
  const s = _formatDateWithWeekday(c.start_date || '');
  const e = _formatDateWithWeekday(c.end_date || '');
  if (!s) return '';
  if (!e) return s;
  const sP = s.replace(/\([^)]*\)$/, '');
  const eP = e.replace(/\([^)]*\)$/, '');
  if (sP === eP) return s;
  return `${s} 至 ${e}`;
}
function _normalizeHours(h) {
  if (!h) return '';
  const n = parseFloat(h);
  if (isNaN(n)) return String(h);
  return n % 1 === 0 ? String(Math.round(n)) : String(n);
}

function renderTable() {
  const kw = document.getElementById('searchKw').value.toLowerCase();
  const institute = document.getElementById('filterInstitute').value;
  const cat = document.getElementById('filterCategory').value;
  const nat = document.getElementById('filterNationality').value;
  const stat = document.getElementById('filterStatus').value;
  const cls = document.getElementById('filterClass').value;

  let visible = allCourses;
  if (kw) {
    const keywords = kw.split(/[,，]/).map(k => k.trim()).filter(Boolean);
    visible = visible.filter(c => keywords.some(k => c.name.toLowerCase().includes(k)));
  }
  if (institute) visible = visible.filter(c => c.institute === institute);
  if (selectedBranches.size > 0) {
    visible = visible.filter(c => selectedBranches.has(c.branch));
  } else {
    visible = [];
  }
  if (cat) visible = visible.filter(c => c.category === cat);
  if (nat) visible = visible.filter(c => c.nationality === nat);
  if (stat === 'open') visible = visible.filter(c => /確定開班|招生|強力/.test(c.status));
  if (stat === 'full') visible = visible.filter(c => /額滿/.test(c.status));
  if (cls) visible = visible.filter(c => c.class_type.includes(cls));

  // 按開課日升冪排序 (近的在前)
  visible.sort((a, b) => (a.start_date || '9999').localeCompare(b.start_date || '9999'));

  document.getElementById('totalCount').textContent = allCourses.length;
  document.getElementById('visCount').textContent = visible.length;
  document.getElementById('selCount').textContent = selected.size;

  const tbody = document.getElementById('tbody');
  if (visible.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:50px;color:#999;cursor:default;">沒有符合條件的課程</td></tr>';
    return;
  }
  tbody.innerHTML = visible.map(c => {
    const isSel = selected.has(c.id);
    const dateStr = _formatDateRange(c);
    const hoursStr = _normalizeHours(c.hours);
    const classCell = c.class_time
      ? `${escHtml(c.class_type)}<br><small style="color:#888;font-size:11px;">${escHtml(c.class_time)}</small>`
      : escHtml(c.class_type);
    return `<tr class="${isSel?'selected':''}" onclick="toggleSel('${escHtml(c.id)}')">
      <td style="text-align:center;"><div class="big-check">${isSel?'✓':''}</div></td>
      <td><strong>${escHtml(c.name)}</strong></td>
      <td>${escHtml(c.branch)}</td>
      <td>${catBadge(c.category)}</td>
      <td>${natBadge(c.nationality)}</td>
      <td>${escHtml(dateStr)}</td>
      <td>${classCell}</td>
      <td style="text-align:center;">${escHtml(hoursStr)}</td>
      <td style="text-align:right;">${escHtml(c.fee)}</td>
      <td>${statBadge(c.status)}</td>
    </tr>`;
  }).join('');
}

function catBadge(c) {
  if (c === '初訓') return '<span class="badge badge-cat-init">初訓</span>';
  if (c === '複訓') return '<span class="badge badge-cat-re">複訓</span>';
  return `<span class="badge badge-other">${c||'?'}</span>`;
}
function natBadge(n) {
  if (n === '本國籍') return '<span class="badge badge-nat-tw">本國</span>';
  if (n) return `<span class="badge badge-nat-fg">${n.replace('籍','')}</span>`;
  return '';
}
function statBadge(s) {
  if (!s) return '<span class="badge badge-other">-</span>';
  if (/額滿/.test(s)) return `<span class="badge badge-full">${s}</span>`;
  if (/確定/.test(s)) return `<span class="badge badge-open">${s}</span>`;
  if (/招生|強力/.test(s)) return `<span class="badge badge-promo">${s}</span>`;
  return `<span class="badge badge-other">${s}</span>`;
}

function toggleSel(id) {
  if (selected.has(id)) selected.delete(id);
  else { const c = allCourses.find(x => x.id === id); if (c) selected.set(id, c); }
  renderTable();
  renderSelected();
}

function renderSelected() {
  const wrap = document.getElementById('selectedList');
  if (selected.size === 0) {
    wrap.innerHTML = '<div style="color:#999;text-align:center;padding:10px;">尚未挑選任何課程</div>';
    return;
  }
  wrap.innerHTML = [...selected.values()].map((c, i) => `
    <div class="sel-item">
      <div class="info">
        <b>${i+1}. ${escHtml(c.name)}</b>
        <div class="meta">
          📍 ${escHtml(c.branch)} ｜ ${escHtml(c.category)} ｜ ${escHtml(c.start_date)} ${escHtml(c.class_type)} ｜ ${escHtml(c.location)} ｜ ${escHtml(c.fee)}元
        </div>
      </div>
      <button class="remove-btn" onclick="toggleSel('${escHtml(c.id)}')">✕ 移除</button>
    </div>
  `).join('');
}

function escHtml(s) {
  return String(s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

async function buildEmail(mode) {
  // 收集多位同仁(每人各自部門)
  const employees = Array.from(document.querySelectorAll('#empList .emp-row')).map(row => ({
    name: row.querySelector('.emp-name-input').value.trim(),
    dept: row.querySelector('.emp-dept-input').value.trim(),
  })).filter(e => e.name);  // 過濾掉沒填名字的空列
  const body = {
    employees: employees,
    emp_month: document.getElementById('empMonth').value || '(月)',
    course_name: document.getElementById('courseName').value || '(課程名稱)',
    train_type: document.getElementById('trainType').value,
    selected_courses: [...selected.values()],
    mode: mode,
  };

  const resp = await fetch('/api/email', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (resp.status === 401) { window.location.href = '/login'; return null; }
  return await resp.json();
}

async function previewExternal() { await preview('external', '信件'); }

async function preview(mode, label) {
  const e = await buildEmail(mode);
  if (!e) return;
  document.getElementById('prevMode').textContent = label;
  document.getElementById('prevSubject').textContent = e.subject;
  document.getElementById('prevBody').innerHTML = e.html;
  document.getElementById('previewModal').classList.add('show');
}

async function generateExternal() { await generate('external', '信件'); }

async function generate(mode, label) {
  if (selected.size === 0) { toast('請先挑選至少一個課程', 'error'); return; }
  const e = await buildEmail(mode);
  if (!e) return;
  try {
    const blob = new Blob([e.html], {type: 'text/html'});
    const data = [new ClipboardItem({'text/html': blob})];
    await navigator.clipboard.write(data);
    toast(`✓ ${label}版已複製! 打開 Outlook 按 Ctrl+V 即可貼上`, 'success');
  } catch (err) {
    await navigator.clipboard.writeText(e.html);
    toast('已複製 (HTML 原始碼)', 'success');
  }
}

// === 多同仁列表管理 ===
function addEmployee(name = '', dept = '') {
  const list = document.getElementById('empList');
  const row = document.createElement('div');
  row.className = 'emp-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = `
    <input type="text" class="emp-name-input" placeholder="姓名,例: 藍若僑" value="${escHtml(name)}" style="flex:1;">
    <input type="text" class="emp-dept-input" placeholder="部門,例: 訓練單位" value="${escHtml(dept)}" style="flex:1;">
    <button type="button" onclick="removeEmployee(this)" style="background:#FFE5E5;border:none;color:#C44569;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:16px;font-weight:bold;flex-shrink:0;" title="刪除這位同仁">✕</button>
  `;
  list.appendChild(row);
}
function removeEmployee(btn) {
  const row = btn.closest('.emp-row');
  if (row) row.remove();
  if (document.querySelectorAll('#empList .emp-row').length === 0) addEmployee();
}
addEmployee(); // 預設先放一列空的

loadCourses();
refreshOnline();
</script>

</body>
</html>
"""


def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    # 雲端部署: PORT 從環境變數讀取 (Render/Heroku/Railway 都這樣)
    port = int(os.environ.get("PORT", PORT))
    is_cloud = "PORT" in os.environ  # 雲端環境會設定 PORT
    
    print("=" * 60)
    print("  外訓課程自動整合工具")
    print("=" * 60)
    
    try:
        init_db()
        print("  [OK] 資料庫初始化完成")
    except Exception as e:
        print(f"  [ERROR] 資料庫初始化失敗: {e}")
        import traceback
        traceback.print_exc()
    
    # 本機才產生「密碼.txt」(雲端不需要,因為沒人看得到雲端檔案)
    if not is_cloud:
        try:
            pwd_file = APP_DIR / "密碼.txt"
            if not pwd_file.exists():
                with open(pwd_file, "w", encoding="utf-8") as f:
                    f.write("【外訓課程整合工具 - 帳號密碼】\n")
                    f.write("=" * 40 + "\n\n")
                    f.write("⚠️  此檔案請勿給其他人看!\n\n")
                    for u in DEFAULT_USERS:
                        f.write(f"帳號: {u['username']}\n")
                        f.write(f"密碼: {u['password']}\n")
                        f.write(f"角色: {u['display_name']} ({u['role']})\n")
                        f.write("-" * 40 + "\n")
                print(f"  [OK] 已產生 [密碼.txt] (請妥善保管)")
        except Exception as e:
            print(f"  [WARN] 無法寫入密碼.txt (略過): {e}")
    
    print("")
    if is_cloud:
        print(f"  [Cloud] 雲端模式啟動,監聽 port {port}")
    else:
        print(f"  伺服器啟動中... http://localhost:{port}")
        print(f"  同事連線網址: http://[你的 IP]:{port}")
        print(f"  (查你的 IP: 開新 cmd → 輸入 ipconfig)")
        print(f"  關閉程式: 按 Ctrl+C 或關閉此視窗")
    print("=" * 60)
    
    # 只在本機才開瀏覽器,雲端不開
    if not is_cloud:
        threading.Thread(target=open_browser, daemon=True).start()
    
    app.run(host="0.0.0.0", port=port, debug=False)


# 雲端 (gunicorn) 不會跑 if __name__ == "__main__",所以下面這段是雲端用的
# gunicorn 直接 import app:app,需要先初始化 DB
try:
    init_db()
    print("[Cloud Init] 雲端模式,資料庫初始化完成")
except Exception as e:
    print(f"[Cloud Init ERROR] {e}")
    import traceback
    traceback.print_exc()
