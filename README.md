# 外訓課程整合管理系統 v7.0

## 📦 檔案說明

| 檔案 | 說明 |
|------|------|
| `app.py` | 主程式 |
| `requirements.txt` | Python 套件清單 (給 Render 看的) |
| `Procfile` | 啟動指令 (給 Render 看的) |
| `.gitignore` | 上傳到 GitHub 時要排除的檔案 |
| `start.bat` | 本機跑用 (雙擊啟動) |
| `雲端部署指南.md` | **重要!** 部署到網路上的詳細步驟 |

## 🎯 兩種用法

### 用法 A：本機跑 (像 v6 那樣)
雙擊 `start.bat`,瀏覽器自動打開 http://localhost:5000

### 用法 B：雲端部署 (給網址讓所有人用)
請看 `雲端部署指南.md`,跟著 step-by-step 做

## 🔐 預設帳號

| 帳號 | 密碼 | 角色 |
|------|------|------|
| train0 | HR123 | 管理員 |
| train1 | 1234 | 使用者1 |
| train2 | 5678 | 使用者2 |
| train3 | 6789 | 使用者3 |

## ✨ v7.0 更新內容

1. ✅ 同帳號單一登入限制 (避免衝突)
2. ✅ 強制登入功能 (踢掉其他裝置)
3. ✅ 多同仁姓名群發 (用逗號分隔: 王小明,陳大華)
4. ✅ 上課地點 bug 修復 (砍掉電話 Email 雜訊)
5. ✅ 系統標語: External Training Management System
6. ✅ 支援雲端部署 (Procfile + requirements.txt)
7. ✅ 啟動畫面不顯示密碼,密碼存在 密碼.txt
