# DocLens 发布指引 (Developer ID + 公证分发)

路线 B: 不上 App Store,走 Developer ID 签名 + Apple 公证(notarization),
DMG 分发。用户下载后双击直接打开,无"未知开发者"警告。

## 一次性准备 (需要你操作, 约 30 分钟)

### 1. 加入 Apple Developer Program
- https://developer.apple.com/programs/ 费用 688 元/年
- 已加入则跳过

### 2. 创建 Developer ID Application 证书
在 Mac 上:
1. 钥匙串访问 → 证书助理 → 从证书颁发机构请求证书 → 存到磁盘(不用填邮箱)
2. developer.apple.com → Certificates → + → **Developer ID Application**
   (注意不是 Installer) 上传刚生成的 .certSigningRequest
3. 下载 .cer 双击导入钥匙串
4. 终端验证:
   ```bash
   security find-identity -v -p codesigning
   # 应出现: "Developer ID Application: 你的名字 (TEAMID)"
   ```

### 3. 创建公证凭据 (App 专用密码)
1. appleid.apple.com → 登录 → App 专用密码 → 生成,记下 `xxxx-xxxx-xxxx-xxxx`
2. 终端存入钥匙串 (TEAMID 在会员页可查):
   ```bash
   xcrun notarytool store-credentials DocLens-notary \
     --apple-id 你的AppleID --team-id TEAMID --password xxxx-xxxx-xxxx-xxxx
   ```

## 日常发布 (全自动, 约 10 分钟)

```bash
cd app

# 1) 重建后端 (改了 server.py/scan.py 后必须)
python3.10 -m PyInstaller server-onedir.spec --noconfirm

# 2) 组装 + Developer ID 签名
DEV_ID="Developer ID Application: 你的名字 (TEAMID)" ./build_dist.sh

# 3) 公证 + staple + DMG
./notarize.sh
```

产物: `dist/DocLens.dmg` — 直接发给用户/传官网。

## 文件说明

| 文件 | 作用 |
|------|------|
| `server-onedir.spec` | PyInstaller onedir 打包(公证友好,无自解压) |
| `entitlements.plist` | Hardened Runtime 宽松项(OpenCV/PyInstaller 必需) |
| `build_dist.sh` | 组装 App + 签名(无 DEV_ID 时 ad-hoc 本地测试) |
| `notarize.sh` | 公证 + staple + DMG + DMG 二次公证 |
| `server.spec` | 旧单文件打包(保留作参考, 公证时可能有兼容问题) |

## 常见问题

- **公证报 invalid signature**: 确认 build_dist.sh 用了 DEV_ID 而非 ad-hoc
- **OpenCV 崩溃 (SIGKILL)**: entitlements.plist 已带 allow-unsigned-executable-memory,
  若仍崩溃再加 `com.apple.security.cs.allow-jit`
- **公证超时**: notarytool --wait 轮询即可,后台排队正常; 也可
  `xcrun notarytool history --keychain-profile DocLens-notary` 查状态
- **Gatekeeper 仍报警**: staple 后要重新压 DMG(脚本已处理顺序)
