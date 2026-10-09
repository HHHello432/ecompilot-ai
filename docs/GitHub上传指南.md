# GitHub 上传指南

这份项目可以直接新建仓库上传。默认模式不依赖真实电商平台密钥，演示数据、文件上传、诊断、日报和前端工作台都能独立运行。

## 上传前检查

```bash
pytest -q
cd frontend/react
npm run build
```

确认不要提交这些内容：

- `.env` 和任何真实 API 密钥
- `backend/storage/platforms/` 内的店铺授权令牌
- 店铺真实订单、商品、客户数据
- `backend/storage/` 本地分析缓存
- `node_modules/`、`dist/`、日志文件

## 新建仓库后推送

在 GitHub 新建空仓库后，在项目根目录执行：

```bash
git init
git config user.name "你的名字"
git config user.email "你的邮箱"
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-name>/<repo-name>.git
git push -u origin main
```

如果你想保留私有业务数据，只上传代码和示例数据即可。真实平台接入建议放在后续分支里完成，并使用 `.env` 或部署平台的密钥管理功能保存授权信息。

## 推荐仓库说明

仓库简介可以写：

```text
EcomPilot AI：面向中小电商商家的运营诊断工作台，支持 CSV/Excel 上传、商品分层、风险队列、AI 问答、日报导出和可选平台连接器扩展。
```
