# 自助下单 + 自动发货商城（Flask + SQLite）

给 John 的 AI 影像服务做的自助下单、自动发货后台。**不是静态站**：有服务器、有数据库、有订单和发货库存。

初始商品（已预置，可在后台任意增删改）：
- AI写真 ¥29 / 6张
- 跳舞视频 ¥49 / 10秒
- 短剧 ¥99 / 10-15秒
- 套餐 ¥69（写真 + 跳舞视频）
- 加急 +¥30

商家邮箱：2100640252@qq.com

## 1. 诚实说明：付款到底验没验证？

**现在没有接通任何真实支付 API，付款没有实时核验。**

流程是诚实版自动发货：
1. 客户选商品、选标准/加急、选微信 / 支付宝 / PayPal。
2. 按页面上的收款码扫码付款（或按收款地址付款）。
3. 客户填写称呼、联系方式、付款时间、交易单号后4位，提交订单。
4. 系统**立即自动发货**：给发货文件下载链接和/或一次性兑换码，订单号同时生成。
5. 订单状态为 **“待核实”**，页面明确写着 **“这不是付款成功”**。商家必须事后在微信 / 支付宝 / PayPal 后台核对到账，核对无误后在后台点“核实通过”，没到账点“驳回”。

为什么不能现在就“真收款后才显示成功”？因为真核验需要商户凭证 + 服务器回调验签，静态站做不了，这个 Flask 站现在也没有这些凭证。不要对外声称“已验证到账”。

### 要启用真核验，需要什么（默认全部关闭，未启用）

后台和 `GET /health` 都显示 `pay_verify_enabled=0`、`pay_verify_mode=disabled`。

| 方式 | 需要店主提供的凭证 | 说明 |
|---|---|---|
| PayPal | PayPal 商家账号的 Client ID、Secret，Webhook ID / Webhook URL | 客户用 PayPal Checkout 付款，PayPal 回调通知服务器，验签通过后才改状态为已付款 |
| 微信支付 | 微信支付商户号（mchid）、APIv3 密钥、商户证书（apiclient_key.pem / 证书序列号）、可公网访问的回调域名 | 需 JSAPI / Native 支付 + 回调验签，个人收款码无法做这个 |
| 支付宝 | 支付宝开放平台应用 AppID、应用私钥、支付宝公钥、回调地址 | 需当面付 / 手机网站支付接口 |

**在提供上述凭证之前，真核验保持关闭。不要把任何密钥、证书、私钥发在聊天里或写进代码；部署时用环境变量或服务器上的安全文件保存。**

## 2. 自动发货怎么工作

- 每个商品有两种发货库存，可二选一或同时用：
  - **发货文件**：后台上传，存在 `data/files/`，客户下单后给下载链接。
  - **兑换码**：后台按行添加，每单消耗一个，**绝不重复使用**，用完即标记已用 + 绑定订单号。
- 有库存就立即发货，状态“待核实”；**无库存则订单为“缺货待处理”，不显示任何下载链接/兑换码**，请商家补货后联系客户。
- 种子数据：AI写真有示例文件 + 2 个示例兑换码，跳舞视频有 1 个示例兑换码，套餐有示例文件，**短剧故意无库存**（用于测试缺货路径）。正式运营前请在后台替换为真实发货文件/兑换码，删除示例库存。

## 3. 本地运行

```bash
cd auto-delivery-store
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash run.sh
# 或直接：.venv/bin/python app.py
```

打开 http://127.0.0.1:5000

- 首页：商品列表
- 商品页：下单表单
- 订单页：`/order/<订单号>`

## 4. 管理员（/admin，首页不显示入口）

- 首次打开 `http://127.0.0.1:5000/admin`：会让你**自己设置一个新管理员密码**（至少 6 位）。密码只以哈希形式存在 SQLite（`data/store.db` 的 `settings.admin_password_hash`），**不写进代码**，代码里没有任何写死密码。
- 之后每次打开 `/admin` 都要输这个密码才能进后台。
- 忘记密码：删除 `data/store.db` 后重建会清空订单和商品，请谨慎；或用 sqlite3 把 `admin_password_hash` 清空后重新设置。
- 后台可以做：改店名/公告；增删改商品和价格；上传微信/支付宝/PayPal 收款码图片或填收款地址；上传发货文件；添加/查看兑换码库存；查看订单、点“核实通过”/“驳回”；导出订单 CSV。

## 5. 部署到 Simular Box（Ubuntu）

Box 现在是 Sleeping 且唤醒失败（No room / Wake failed），**先不要部署，等 Box 能唤醒成 Running 再做**：

```bash
# 1. 把整个 auto-delivery-store 文件夹复制到 Box 的 /workspace/
# 2. 在 Box 终端里：
cd ~/workspace/auto-delivery-store   # 或 /workspace/auto-delivery-store
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash run.sh
# 3. 保持 Box 醒着：Box 一休眠，网站就下线。免费计划每天 1000 credits，00:00 UTC 重置。
# 4. 首次在 Box 上打开 http://<Box内网或隧道地址>:5000/admin 设置管理员密码。
```

数据都在 `data/` 里：`data/store.db`（订单/商品/密码哈希）、`data/files/`（发货文件）、`data/qr/`（收款码图片）。备份时把整个 `data/` 复制走即可。

## 6. 免费公开地址（cloudflared 隧道）

Box 后台**不提供**公开 URL、公网 IP、端口转发。只能用隧道：

```bash
# 在 Box 上另开一个终端（网站本身仍在 5000 端口运行）：
cloudflared tunnel --url http://localhost:5000
# 会给一个 https://xxx.trycloudflare.com 这样的免费随机地址，客户用这个地址访问。
# 注意：每次重启隧道地址都会变；Box 休眠或 cloudflared 退出后地址失效。
# 如果 Box 上没有 cloudflared，先装：参考 https://developers.cloudflare.com/cloudflare-one/connections/connect/downloads/
# ngrok 是备选，免费版同样是随机地址、需账号。
```

## 7. Box 的限制（请先接受再上线）

- Box 会休眠，休眠后网站下线，客户打不开；每天 1000 credits，用完要等次日 00:00 UTC 重置。
- 定时唤醒是每个工作日 09:00（Asia/Shanghai），持续 1 小时。
- 隧道地址重启会变，不适合印在固定海报上长期用；要固定域名需另购域名 + 固定隧道配置。
- 没有商户凭证前，付款只能做到“提交凭证后自动发货 + 待核实”，不能做到“实时验签到账后才发货”。

## 8. 文件清单

```
auto-delivery-store/
  app.py            # 全部后端 + 页面（Flask）
  requirements.txt  # 只有 Flask
  run.sh            # 启动脚本，0.0.0.0:5000
  README.md         # 本文件
  data/
    store.db        # 首次运行时自动创建并播种
    secret.key      # 首次运行时随机生成（不在代码里）
    files/          # 发货文件
    qr/             # 收款码图片
```

## 9. 测试（本地已跑过，见下文报告）

- `GET /api/products` 列出 4 个商品
- 下单 ¥29 商品：返回订单页，含下载链接/兑换码，状态“待核实”，全站无“付款成功”字样
- 下单无库存的短剧：状态“缺货待处理”，无发货内容
- `/admin` 首次设密码、登录、后台操作、导出 CSV
