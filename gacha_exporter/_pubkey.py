"""签名公钥副本（app.zip 源码版）—— 由 签名密钥生成脚本 生成，勿手改。

开发（非打包）运行时，验签从这里读公钥；打包运行时 bootstrap.py 以
packaging/embedded_pubkey.py 的值为准预置 sys.modules["gacha_exporter._pubkey"]，
zip 内本模块被覆盖——即使 app.zip 被替换为伪造包，验签锚点仍以 exe 内嵌为准。
"""

PUBLIC_KEY_B64 = "Dw1kDvvYHQtm5l1GDNMFA2u1dAKKgNcH32hq2xjKp0Y="
