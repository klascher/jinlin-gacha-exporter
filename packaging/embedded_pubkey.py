"""exe 内置签名公钥（发行锚点）—— 由 签名密钥生成脚本 生成，勿手改。

bootstrap 用它作为独立信任根：发版时 pack_release 用签名私钥签 app.zip，
客户端下载后用此公钥验签，签名无效的补丁一律拒绝。私钥在开发机
（仓库外 ../pem/signing.key，不入库），公钥打进 exe + app.zip
源码副本 gacha_exporter/_pubkey.py。打包运行时 bootstrap 以本文件为准预置
sys.modules["gacha_exporter._pubkey"]，覆盖 zip 内同名模块——即使 app.zip
被替换为伪造包，验签锚点仍以 exe 内嵌为准。

轮换此公钥 = 重打包 exe（极罕见事件，见 打包更新说明）。
"""

PUBLIC_KEY_B64 = "Dw1kDvvYHQtm5l1GDNMFA2u1dAKKgNcH32hq2xjKp0Y="
