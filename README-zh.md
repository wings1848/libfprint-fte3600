<div align="center">

# libfprint-fte3600

**面向 FocalTech `FTE3600` 系列 SPI 指纹传感器的 Linux 驱动、ACPI/DKMS 内核胶水模块与免提权 GTK4 / CLI 工具箱**

[English](README.md) · [简体中文](README-zh.md)

[![CI](https://github.com/wings1848/libfprint-fte3600/actions/workflows/fte3600-ci.yml/badge.svg)](https://github.com/wings1848/libfprint-fte3600/actions/workflows/fte3600-ci.yml)
[![AUR bin](https://img.shields.io/aur/version/libfprint-fte3600-bin?label=AUR%20libfprint-fte3600-bin)](https://aur.archlinux.org/packages/libfprint-fte3600-bin)
[![AUR git](https://img.shields.io/aur/version/libfprint-fte3600-git?label=AUR%20libfprint-fte3600-git)](https://aur.archlinux.org/packages/libfprint-fte3600-git)
[![License: LGPL-2.1-or-later](https://img.shields.io/badge/host%20%26%20toolkit-LGPL--2.1--or--later-blue.svg)](COPYING)
[![License: GPL-2.0-only](https://img.shields.io/badge/kernel%20glue-GPL--2.0--only-green.svg)](kernel/fte3600/fte3600.c)

[快速开始](#快速开始) · [客户端与日常使用](#客户端与日常使用) · [硬件支持](#硬件支持) · [组件结构](#组件结构) · [文档导航](#文档导航)

</div>

---

> [!IMPORTANT]
> - **仅支持 SPI 传感器**：USB 接口的 FocalTech 指纹设备不在本驱动支持范围内。
> - **认证阈值尚处于实验阶段**：BRISK 匹配阈值尚未经过人群级 FAR/FRR 校准（`FTE3600_BRISK_THRESHOLDS_CALIBRATED=0`），**请始终保留系统密码登录方式**。
> - **不内置闭源固件**：若传感器芯片需要外部固件，请自行准备并按说明安装（见 [install.md](docs/fte3600/install.md#firmware)）。

## 项目概述

`libfprint-fte3600` 为采用 FocalTech `FTE3600` 系列 SPI 按压式指纹传感器的便携设备（如 GPD Pocket 3、One-Netbook A1、OneMix 4、ONEXPLAYER X2 Mini 等）提供标准的 Linux `libfprint` / `fprintd` / PAM 指纹认证支持。

已实现的驱动型号档位包括：`FT9338`、`FT9348`、`FT9361`、`FT9536`、`FT9365`、`FT9368`、`FW9369` 与 `FT9769`。

## 组件结构

| 组件 | 路径与许可证 | 职责与特性 |
| --- | --- | --- |
| **ACPI / DKMS 内核胶水模块** | [`kernel/fte3600`](kernel/fte3600)（`GPL-2.0-only`） | 最小化 ABI 2 平台模块，负责暴露 ACPI 复位 GPIO 与 UIO 边沿中断，并配合发行版自带 `spidev`（`bufsiz >= 65536`）工作。 |
| **`libfprint` SPI 驱动** | [`libfprint/drivers/fte3600`](libfprint/drivers/fte3600)（`LGPL-2.1-or-later`） | 用户态 SPI 协议栈，支持符合条件的旧型号快速唤醒（`FT9338`/`FT9348`/`FT9361`/`FT9536`）、可取消冷启动校准重试、抬手超时重开冷却保护、验证/识别休眠唤醒（`.suspend`/`.resume`）、12 样本模板库、小面积（`<= 64×80`）传感器覆盖录入，以及严格单次 LCN 的 `sam7-brisk` 匹配。 |
| **免提权工具箱（CLI + GUI）** | [`toolkit/`](toolkit)（`LGPL-2.1-or-later`） | 基于同一套 `fprintd` `Gio.DBus` 状态机的命令行工具 `fte3600-toolkit` 与单窗口 GTK4 + libadwaita 图形工具 `fte3600-toolkit-gui`：仅在 `Claim` + `Start` 就绪后提示按压；在指纹与电源键复用的 GPD 设备上自动申请 `systemd-logind` `handle-power-key` 抑制锁；提供只读环境体检（`doctor`）与不含生物特征的 JSONL 耗时统计（`report`）。 |
| **离线多算法基准实验室** | [`tools/fte3600-bench`](tools/fte3600-bench)（`LGPL-2.1-or-later`） | 独立 Docker 评测环境，直接编译链接仓库内真实的 `sam7-brisk` C 核心，在合成压力集或私有 `FTE3600_DUMP_DIR` 帧集上对比 `BRISK`、`CLAHE+BRISK`、`AKAZE` 与 `SIGFM(SIFT)`，无需在宿主机安装 OpenCV。 |

## 硬件支持

| 设备型号 | 传感器 / 芯片 | 验证状态与参考 |
| --- | --- | --- |
| **GPD Pocket 3（`G1621-02` / N6000）** | `FW9369`，原始 ID `0x9362`（`64×80`，无需外部固件） | 已验证：12 样本覆盖录入、连续识别验证、PAM `sudo`、S3 休眠唤醒与锁屏解锁（见 [GPD Pocket 3 指南](docs/fte3600/hardware-gpd-pocket3.md)、[上游报告 #2](https://github.com/SamSeven777/libfprint-fte3600/issues/2#issuecomment-6212129012)）。 |
| **One-Netbook A1** | `FT9361` | 上游原始测试机硬件确认可用。 |
| **OneMix 4** | `FT9769` 系列，芯片 `0x9391`，ACPI `FTE6600` | 重新录入与识别已确认可用（[上游 #11](https://github.com/SamSeven777/libfprint-fte3600/pull/11)）。 |
| **ONEXPLAYER X2 Mini** | `FT9365`，芯片 `0x9365` | 社区报告录入与解锁可用（[上游 #15](https://github.com/SamSeven777/libfprint-fte3600/pull/15)）。 |

如设备未在表中列出，可使用 `fte3600-tool`（见 [硬件工具文档](docs/fte3600/tools.md)）探测芯片型号并收集诊断信息。

## 快速开始

### Arch Linux（AUR 推荐安装）

推荐安装由 GitHub Actions 预编译的 [`libfprint-fte3600-bin`](https://aur.archlinux.org/packages/libfprint-fte3600-bin)（跳过本地 `libfprint` 编译，仅在本地通过 DKMS 编译内核胶水模块），或从源码构建 [`libfprint-fte3600-git`](https://aur.archlinux.org/packages/libfprint-fte3600-git)。请一并确保已安装 `fprintd` 及当前内核对应的头文件包（如 `linux-headers` 或 `linux-lts-headers`）；如需桌面图形界面，安装 `gtk4` 与 `libadwaita`：

```sh
# 预编译二进制包（推荐，低功耗设备秒装）：
paru -S libfprint-fte3600-bin fprintd linux-headers

# 或从最新 main 分支源码编译：
paru -S libfprint-fte3600-git fprintd linux-headers

# 可选：启用桌面图形工具箱
paru -S gtk4 libadwaita
```

重启系统使 DKMS 加载 `fte3600` 模块并应用 `spidev` 的 `bufsiz=65536` 参数，随后授权 `fprintd` 访问设备节点并运行体检：

```sh
sudo fte3600-device-allow --install
sudo systemctl restart fprintd.service
# 可选（电源键复用指纹的机型，如 GPD Pocket 3）：启用电源键共存守护进程
sudo systemctl enable --now fte3600-power-guard.service
fte3600-toolkit doctor
```

### 源码构建（Fedora、Ubuntu、NixOS 及其他发行版）

完整的依赖清单与 NixOS 配置说明见 [install.md](docs/fte3600/install.md) 与 [packaging/README.md](packaging/README.md)。

```sh
# 1. 安装内核胶水模块并检查硬件配对
sudo ./scripts/setup-fte3600.sh install-kernel
./scripts/fte3600-tool.sh check

# 2. 编译并安装 libfprint 与工具箱
meson setup build --prefix=/usr --buildtype=release -Ddrivers=all -Ddoc=false -Dgtk-examples=false
meson compile -C build
sudo meson install -C build
python3 -m pip install --no-build-isolation ./toolkit
```

## 客户端与日常使用

安装完成后提供 3 种面向终端用户的客户端，日常使用无需手动运行仓库脚本或记忆复杂参数：

| 客户端 | 适用场景 | 启动方式 | 说明 |
| --- | --- | --- | --- |
| **系统原生设置（KDE / GNOME）与 PAM** | 日常解锁、`sudo`、锁屏认证 | 「KDE 系统设置 → 用户 → 配置指纹认证」 | 通过标准 `libfprint-2.so` / `fprintd` 原生接入。 |
| **桌面图形工具箱（`fte3600-toolkit-gui`）** | 引导式录入、防电源键误触、识别率测试 | 应用菜单打开 **FTE3600 Fingerprint Toolkit**，或终端直接运行 `fte3600-toolkit-gui` | 零参数启动；等待传感器就绪后再提示按压，并自动抑制 GPD 电源复用键短按动作。 |
| **命令行工具（`fte3600-toolkit`）** | 终端与 SSH 环境 | `fte3600-toolkit doctor`<br>`fte3600-toolkit enroll`<br>`fte3600-toolkit verify`<br>`fte3600-toolkit report` | 子命令均内置常用默认值（右手食指、5 轮验证）。只读查询与 `verify` 无需密码；`enroll` / `delete` 会触发 Polkit 授权（纯 SSH 下请使用 `sudo fte3600-toolkit enroll --user "$USER"`）。 |

## 验证与测试

```sh
# C 驱动、syscall 拦截运行时测试、伪硬件后端公共 API 测试与内核策略检查
./scripts/check-fte3600.sh

# 工具箱单元测试与隔离会话总线 D-Bus 端到端测试
python3 -W error::ResourceWarning -m unittest discover -s toolkit/tests -p 'test_core_unit.py' -v
dbus-run-session -- python3 -W error::ResourceWarning -m unittest toolkit/tests/test_dbus_e2e.py toolkit/tests/test_gui_smoke.py -v

# 可选：运行 Docker 四算法离线基准（BRISK / CLAHE+BRISK / AKAZE / SIGFM）
./tools/fte3600-bench/run-docker.sh
```

## 文档导航

| 文档 | 内容说明 |
| --- | --- |
| [docs/README.md](docs/README.md) | 文档总览与路线图 |
| [docs/fte3600/install.md](docs/fte3600/install.md) | 源码编译安装、固件放置、PAM 配置与回滚 |
| [packaging/README.md](packaging/README.md) | Arch Linux AUR（`libfprint-fte3600-bin` / `-git`）与 NixOS 打包说明 |
| [docs/fte3600/hardware-gpd-pocket3.md](docs/fte3600/hardware-gpd-pocket3.md) | GPD Pocket 3（`G1621-02` / N6000）硬件说明与电源键复用配置 |
| [docs/fte3600/toolkit.md](docs/fte3600/toolkit.md) | `fte3600-toolkit` CLI 与 GTK4/libadwaita GUI 说明与统计格式 |
| [docs/fte3600/matcher-benchmark.md](docs/fte3600/matcher-benchmark.md) | Docker 多算法离线基准测试与真机实测结论 |
| [docs/fte3600/architecture.md](docs/fte3600/architecture.md) | 内核胶水、驱动、`fprintd` 与工具箱整体架构 |
| [docs/fte3600/tools.md](docs/fte3600/tools.md) | `fte3600-tool` 硬件探测与冷启动耗时测试 |
| [docs/fte3600/fast-open-validation.md](docs/fte3600/fast-open-validation.md) | 旧型号快速唤醒验证标准 |
| [docs/fte3600/troubleshooting.md](docs/fte3600/troubleshooting.md) | 常见问题排查（唤醒盲区、电源键冲突、Polkit/SSH、休眠恢复） |

## 分支与匹配算法

- `main` 分支采用单次局部对比度归一化（13×13 LCN）的 `sam7-brisk` 匹配器执行录入、验证与识别。
- 实验性 IPA 匹配器源码保存在上游 [`research/algorithms`](https://github.com/SamSeven777/libfprint-fte3600/tree/research/algorithms) 分支。

## 许可证与致谢

- 用户态驱动与工具箱：`LGPL-2.1-or-later`
- 内核 ACPI/UIO 胶水模块（`kernel/fte3600`）：`GPL-2.0-only`
- 本项目基于上游 [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint) 与 [SamSeven777/libfprint-fte3600](https://github.com/SamSeven777/libfprint-fte3600)，并集成经署名授权的 SamSeven777 BRISK 实现。详见 [independent-source.json](docs/fte3600/independent-source.json)、[COPYING](COPYING) 与 [`sam7-brisk/UPSTREAM.json`](libfprint/drivers/fte3600/sam7-brisk/UPSTREAM.json)。
- 仓库规范：[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

### 上游贡献者

- [@FlightDan](https://github.com/FlightDan) · [@solarkennedy](https://github.com/solarkennedy) · [@ttamakijp](https://github.com/ttamakijp) · [@iwansugiarto](https://github.com/iwansugiarto) · [@Tuxman2](https://github.com/Tuxman2) · [@Kriketo](https://github.com/Kriketo) · [@Evaner5580](https://github.com/Evaner5580) · [@shokerplz](https://github.com/shokerplz)
