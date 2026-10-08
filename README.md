# 金鳞抽卡导出工具

一款运行在 Windows 桌面端的抽卡记录抓取与统计工具：通过本地抓包解析游戏的抽卡历史数据，
自动合并去重入库，提供保底进度、出货率、不歪率等统计视图，并支持导出 Excel 备份。

> 本工具为非官方个人研发项目，与游戏官方无关；仅供个人学习研究使用，**禁止商业用途**。

## 功能特性

- **抓取抽卡记录**：基于本地数据包嗅探解析抽卡历史（依赖 [Npcap](https://npcap.com)），
  支持十连/单抽跨页去重、新旧翻页方向无关的记录重建
- **自助获取抽卡地址**：无需手动抓包分析，工具内按协议特征自动识别抽卡服务器地址，
  识别结果按账号持久化（真实地址因账号/环境而异，工具不内置）
- **保底统计**：共享保底分组的抽数计数与小保底/大保底状态跟踪、硬保底进度
- **出货统计**：UP 出货率、不歪率（含大保底必出口径），按卡池 / **按角色** 出货流水视图
- **多账号档案**：抓包结果按账号隔离存储，历史自动合并去重
- **数据维护**：手动补齐缺失记录、单池还原、校验预览
- **导出 Excel**：一键导出全部抽卡记录为 xlsx 备份
- **数据同步文件**：生成跨端同步文件，供移动端等其它工具导入

## 环境要求

- Windows 10 / 11（抓包依赖 Npcap，安装时勾选「Install Npcap in WinPcap API-compatible Mode」）
- Python 3.11+

## 从源码运行

```bash
pip install -r requirements.txt
python -m gacha_exporter
```

首次启动会引导安装 Npcap 并初始化本地档案。

## 使用说明

1. 启动后选择或新建账号档案；
2. 点击「自助获取抽卡地址」，按引导完成一次识别（识别成功后按 (ip, port) 精确过滤，无需重复操作）；
3. 点击「开始抓包」，在游戏内打开抽卡历史页并翻页，工具实时解析入库；
4. 完成后点击「停止抓包」，在统计面板查看保底与出货数据。

所有抽卡数据仅保存在本机（`gacha_exporter/data/` 下的 users / cache / logs 子目录），
工具不上传任何抽卡记录。

## 元数据配置（**重要，请先读**）

界面里把卡池 / 角色 ID 显示成名字，依赖一份**元数据**（角色名、稀有度、卡池与保底组配置）。

**本仓库是源码公开版，不含服务端凭据**：`gacha_exporter/loaders/remote_data.py` 里的
接口地址 / KEY / SECRET 均为**占位值**：

```python
DEFAULT_REMOTE_URL = "https://example.com/v1/role"
DEFAULT_KEY = "example-key"
DEFAULT_SECRET = "example-secret-not-real"
```

因此**开箱状态下拉不到元数据**，统计页会以 ID 显示。想让名字显示出来，任选一种：

1. **自行配置数据源**：用环境变量指定你自己的服务端（优先级高于上面三个常量）：

   ```bash
   set JINLIN_ROLE_URL=https://你的服务端/v1/role
   set JINLIN_KEY=你的key
   set JINLIN_SECRET=你的secret
   ```

   服务端需按同套鉴权约定实现（HMAC-SHA256 签名，见 `gacha_exporter/utils/auth.py`）。

2. **离线放置**：把符合解析层结构的 JSON 放到
   `gacha_exporter/data/cache/role_cache.json`（远程拉取失败时自动回退读取它）。

> 说明：公开仓库里的凭据**无法保密**（源码谁都能看），所以这类凭据不宜由作者代替
> 使用者保管；请使用你自己的数据源。

## 开发与测试

```bash
pip install -r requirements.txt
python -m pytest tests/
```

目录结构：

```
gacha_exporter/   客户端核心（capture 抓包 / analysis 解析统计 / gui 界面 / storage 存储 / exporters 导出）
packaging/        打包与更新链（app.zip 构建、exe 构建、发布打包）
tests/            单元测试与金标准回归样本
```

## 许可证

本项目采用 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) 授权。

    SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
    Required Notice: Copyright (c) 2026 klascher

## 反馈

问题与建议请前往 [GitHub Issues](https://github.com/klascher/jinlin-gacha-exporter/issues) 提交。
