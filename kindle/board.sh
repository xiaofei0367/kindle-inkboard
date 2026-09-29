#!/bin/sh
# Kindle 看板主逻辑 v5.8 —— 深度睡眠省电版 + 防卡死 + USB 安全 + 不碰 WiFi + 计时修正
# 由 /mnt/us/documents/BoardUpgrade.sh（或 BoardStart.sh）调用；幂等，点一次 = 重启一次
# 部署：D:\board\board.sh.new -> Kindle 侧 cp -f -> /mnt/us/board/board.sh
#
# ── v5.8 (2026-09-24) 修「被外部唤醒被误判为睡眠失败」+ 唤醒瞬间立即补刷 ──
#   依据 v5.7 实测（15:42:48）：用户插 USB -> USB 插入本身就是唤醒源 ->
#     设备从 3540s 的睡眠里只睡了 554s 就醒 -> 我的判据把它当「没睡下去」，
#     误记一次 SLEEP FAIL。⚠️ 连续 3 次就会**自动停用睡眠功能** ——
#     而「按电源键看屏」也是同样的唤醒源 ⇒ 这是个真实隐患。
#   ① 判据改为两段（★ 核心）：
#        实睡 < MIN_SLEEP(30s)          -> 真故障（echo mem 被拒/没进 suspend）-> 计数+1
#        30s <= 实睡 < 闹钟*0.75        -> 被外部唤醒（USB/电源键），**不计失败**
#   ② 被打断唤醒时**立刻重刷当前图**（不等联网）：
#      framework 唤醒后会画锁屏盖住看板，而 ensure_wifi+wget 要 5~20s，
#      用户按电源键看屏时等太久。先 eips 一下最快。
#
# ── v5.7 (2026-09-24) 修「故障路径仍会关 WiFi」+ 长期故障省电 ──────
#   依据 v5.6 实测（wifi_diag 快照）：
#     · wifid cmState = PENDING（永远到不了 CONNECTED）、wlan0 TX packets = 0
#       （一个包都没发出去）、信号 -56dBm 良好、wirelessEnable = 1
#     · 13:59 的整机重启（PID 回绕 32149->1626、batt.sh 死亡）也没救回来
#   ⇒ ① **主动关 WiFi 是风险源，不是救援手段**。v5.6 只改了"正常路径不关"，
#        但 ensure_wifi 的**故障恢复路径仍在做「关/开重置」**——自相矛盾，
#        且在已 wedge 时是火上浇油。→ 本次加 WIFI_RECOVER（默认 0 = 绝不主动关）。
#      ② WiFi 长期断时每轮白等 30~95s 且保持 Active = 纯耗电。
#        → 连续失败 >=3 轮后**跳过等待与拉图直接睡**。
#
# ── v5.6 (2026-09-24) 再修两个 ──────────────────────────────────────
#   ① ★根因：`wirelessEnable 0` + suspend → WiFi 再也回不来（v5.4 实测 7 轮全失败，
#      v5.5 的「关/开重置」也救不回 ⇒ wifid/driver 层 wedge，不是"没触发关联"）。
#      ⇒ **不再关 WiFi**：WIFI_OFF 默认 1 -> **0**。
#        代价：suspend 期间被 AP 的 DTIM beacon 周期性唤醒，耗电上升；
#        收益：不再破坏 WiFi —— 正常 Kindle 就是"WiFi 开着睡、醒来自己连回来"。
#   ② 计时 bug：闹钟原在轮次开头设，ensure_wifi 烧掉的 ≤95s 会直接从睡眠里扣
#      （实测 IVAL=120 时只睡 13s）⇒ 闹钟改到**睡前这一刻**精确设。
#   ③ WiFi 故障时 dump 快照（ifconfig/route/关联）进 sleep.log，把黑盒变白盒。
#
# ── v5.5 (2026-09-24) 修 v5.4 实测暴露的三个问题 ────────────────────
#   ① ★ WiFi 恢复不可靠：v5.4 第一次「关WiFi -> 睡 -> 开WiFi」之后，
#      WiFi 再也没连上（13:38 起连续 7 轮 wget 全失败，每次 65s 空转）。
#      → 新增 ensure_wifi()：不靠"重试 wget"，而是**等到 wlan0 真的有 IP**；
#        30 秒拿不到就做一次「关/开」重置（强制 wifid 重新初始化）再等 60 秒。
#   ② ★ 失败比成功还耗电（设计缺陷）：v5.4 拉图失败时 can_sleep=0
#      → 设备保持 Active + WiFi 常开 15 分钟 ≈ 66mAh ≈ 6% 电量。
#      → 解耦：**睡眠是默认行为**，失败只**缩短本轮睡眠时长**（退避）；
#        且失败轮**不关 WiFi**（让它有时间自己关联上）。
#   ③ 首轮日志误报「已脱离 USB/充电」：`_usb` 未初始化。
#      → `_usb="0"` 初始化。
#   另：新增 board.conf 的 WIFI_OFF 开关。
#
# ── v5.4 (2026-09-24) USB 模式整轮挂起 ─────────────────────────────
#   USB 大容量存储模式下 framework 暂停 -> lipc-* 与 eips 都可能永久阻塞
#   （v5 卡死是 wget，这是同源第二条路）。检测走纯 sysfs，命中则整轮挂起。
#
# ── v5.2 (2026-09-24) 修「整轮卡死」─────────────────────────────────
#   busybox wget 无超时 -> 插着 USB 时 WiFi 处于「已连接但通路断」中间态
#   -> TCP 请求杳无音信 -> wget 永久阻塞 -> 整轮冻死。
#   修：wget -T 20 硬超时 + 90 秒墙钟预算 + 心跳 .hb + 看门狗 board_watch.sh。
#
# ── v5   (2026-09-24) 治本：从「永不睡」改成「拉完图就睡」──────────
#   病根：v4 用 preventScreenSaver=1 按住设备不让进屏保 -> 永远 Active。
#         实测 current_now = -265 mA (约 0.95W)，满电只撑约 4.2 小时。
#   实测（sleep test）：/sys/power/state 支持 mem、rtc0/wakealarm 可写、rtcwake 在、
#         无 wake_lock 阻挡、真睡了 85s 被 RTC 唤醒、醒来后 wget + eips 全部正常。
#   v5.4 实测：真睡 169s（闹钟 180s），睡前 -76.7mA。
#   三护栏：① 闹钟先设后干活、回读为空就不睡（宁可耗电绝不睡死）
#           ② 连续 3 轮没真睡 -> 自动退回常驻模式
#           ③ board.conf 里 SLEEP=0 一秒回退

BOARD=/mnt/us/board
CONF=$BOARD/board.conf
PIDF=$BOARD/loop.pid
WPIDF=$BOARD/watch.pid
LOOP=$BOARD/loop.sh
WATCH=$BOARD/board_watch.sh
LOG=$BOARD/board.log
mkdir -p $BOARD

# 日志保护：超过 256KB 只留最后 400 行
if [ -f "$LOG" ]; then
  LSZ=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  if [ "$LSZ" -gt 262144 ]; then
    tail -n 400 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    echo "[board] 日志已截断（原 ${LSZ}B）" >> "$LOG"
  fi
fi

echo "[board] ================= $(date '+%Y-%m-%d %H:%M:%S') ================="

# ── 参数文件：只在不存在时写默认值（绝不覆盖已调好的）────────────
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<'EOC'
# Kindle 看板可调参数 —— 改完保存即可，循环下一轮自动生效（无需重启动）
# 本文件在电脑上是 D:\board\board.conf，在 Kindle 上是 /mnt/us/board/board.conf

# URL   看板图片地址（渲染机推送后供 Kindle 拉取）
URL=http://YOUR_SERVER_IP/board.png

# SLEEP 深度睡眠省电模式
#       1 = 拉完图关 WiFi 并 echo mem 睡到下一轮（省电主力，满电预计 3 周量级）
#       0 = 老行为，一直 Active 不睡（满电约 4 小时，仅在睡眠出问题时回退用）
SLEEP=1

# WIFI_OFF 睡前是否关 WiFi
#       0 = 不关（★ v5.6 默认。实测「关WiFi+睡」之后 WiFi 再也回不来，
#                 v5.5 的「关/开重置」也救不回 ⇒ 不碰它最安全）
#       1 = 关（更省电：避免 suspend 期间被 AP 的 DTIM beacon 周期性唤醒；
#               但实测会破坏 WiFi，**除非已确认恢复机制可靠，否则别开**）
#       ★ 无论此值如何，拉图失败的那一轮都不会关 WiFi
WIFI_OFF=0

# WIFI_RECOVER WiFi 连不上时是否允许「主动关再开」来救（v5.7 新增）
#       0 = 不允许（★默认。实测「关WiFi」会让 wifid 卡在 PENDING 且重启都救不回，
#                所以默认只做 enable 1 + 等待，绝不再主动关它）
#       1 = 允许做一次「关/开重置」（更激进；仅在确认本机 WiFi 能扛住时开）
WIFI_RECOVER=0

# POLL  轮询间隔（秒）= 睡眠时长。公司机任务每 15 分钟(=900s)出图一次。
#       绝不能是 900 的整数倍（900/1800/3600…）会与上游严格同频、长期慢一拍。
#       3540(59分钟) 与 900 的 gcd=60，相位每轮错开 60s。
POLL=3540

# KEEP  连续多少轮"图没变"后强制重刷。睡眠模式下已无意义
#       （每轮都从睡眠唤醒，醒来必刷一次）。保留仅为 SLEEP=0 兜底自愈用。
KEEP=1

# BATT_X / BATT_Y  电量 "NN%" 在屏上的位置（eips 文本网格坐标，需真机微调）
#       右上角电量百分比摆放；位置/方向不对就调这两个数（或交换）。
#       也可直接加进 D:\board\board.conf，循环每轮重读，保存即生效。
#       2026-09-29 v14 上线：Y=4 落 Kindle 系统状态栏 45px 禁区，挪到 Y=6
#       （≈66-77px，v14 顶栏右上留白带：45px 以下、「今日用量」y=92 以上）。
BATT_X=68
BATT_Y=6
EOC
  echo "[board]    已生成默认参数文件 $CONF"
fi

echo "[board] 1) 环境探测"
for c in eips wget setsid nohup cmp lipc-set-prop crond mntroot rtcwake tr ifconfig; do
  printf "[board]    %-14s %s\n" "$c" "$(command -v $c 2>/dev/null || echo MISSING)"
done
echo "[board]    wakealarm      $(ls /sys/class/rtc/rtc0/wakealarm 2>/dev/null || echo MISSING)"
echo "[board]    power states   $(cat /sys/power/state 2>/dev/null)"
echo "[board]    usb present    $(cat /sys/class/power_supply/imx_usb_charger/present 2>/dev/null || echo NA)"
echo "[board]    wlan0 有 IP?   $(ifconfig wlan0 2>/dev/null | grep -q 'inet addr' && echo YES || echo NO)"
echo "[board]    wget 支持 -T   $(wget -T 1 -q -O /dev/null http://127.0.0.1:1/ 2>&1 | grep -qi 'invalid\|unrecognized' && echo '不支持(!!)' || echo OK)"

echo "[board] 2) 休眠策略（由循环按 SLEEP 参数每轮动态管理）"
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
echo "[board]    当前 preventScreenSaver -> $(lipc-get-prop com.lab126.powerd preventScreenSaver 2>&1)"

# ★ 通用清杀：读 /proc/<pid>/cmdline（busybox 的 ps 只显示 "sh"，grep 脚本名恒匹配不到）
kill_by_cmd() {
  PAT=$1
  MYSELF=$$
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [ "$p" = "$MYSELF" ] && continue
    CMD=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
    case "$CMD" in
      *$PAT*)
        echo "[board]    清理残留 $PAT pid=$p"
        kill -9 "$p" 2>/dev/null
        ;;
    esac
  done
}

echo "[board] 3) 停掉旧的循环与看门狗（幂等）"
if [ -f "$PIDF" ]; then
  OLD=$(cat "$PIDF" 2>/dev/null)
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "[board]    旧循环 pid=$OLD -> kill -9"
    kill -9 "$OLD" 2>/dev/null
    sleep 1
  else
    echo "[board]    旧循环 pid=$OLD 已不在"
  fi
  rm -f "$PIDF"
fi
if [ -f "$WPIDF" ]; then
  OLDW=$(cat "$WPIDF" 2>/dev/null)
  if [ -n "$OLDW" ] && kill -0 "$OLDW" 2>/dev/null; then
    echo "[board]    旧看门狗 pid=$OLDW -> kill -9"
    kill -9 "$OLDW" 2>/dev/null
  fi
  rm -f "$WPIDF"
fi
kill_by_cmd "board/loop.sh"
kill_by_cmd "board/board_watch.sh"
rm -f "$BOARD/.hb"
sleep 1

echo "[board] 4) 写循环脚本 $LOOP"
cat > "$LOOP" <<'EOS'
#!/bin/sh
# ── 电量叠加（方案 B：eips 文字，不依赖服务端）──
BATT_X=${BATT_X:-68}
BATT_Y=${BATT_Y:-6}
read_batt() {
  b=""
  b=$(cat /sys/class/power_supply/*/capacity 2>/dev/null | head -1)
  case "$b" in ''|*[!0-9]*) echo "";; *) echo "$b";; esac
}
show_board() {
  eips -f -g "$CUR" >/dev/null 2>&1 || return 1
  B=$(read_batt)
  [ -n "$B" ] && eips $BATT_X $BATT_Y "${B}%" 2>/dev/null || true
  return 0
}
# Kindle 看板循环 v5.5（由 board.sh 生成，不要手工改这里；改 board.sh）
# 设计要点：① 任何环节都不许无限阻塞 ② 心跳+看门狗双保险
#           ③ 睡眠是默认行为（失败只缩短睡眠，不停留在 Active）
#           ④ WiFi 恢复以「真的有 IP」为准，不靠盲重试
BOARD=/mnt/us/board
CONF=$BOARD/board.conf
NEW=$BOARD/new.png
CUR=$BOARD/board.png
SLOG=$BOARD/sleep.log
SFAIL=$BOARD/.sleep_fail
WFAIL=$BOARD/.wifi_fail
HB=$BOARD/.hb
PIDF=$BOARD/loop.pid
WPIDF=$BOARD/watch.pid
WATCH=$BOARD/board_watch.sh

URL=http://YOUR_SERVER_IP/board.png
POLL=3540
KEEP=1
SLEEP=1
WIFI_OFF=0
WIFI_RECOVER=0
FASTP=180          # 自检用短周期：开机头 3 轮用 3 分钟，验完自动转 POLL
RETRY=60           # 拉图失败后的基准重试间隔（退避按 2 倍递增，上限 POLL）
WGET_TO=20         # wget 单次硬超时（秒）—— 治 v5 的卡死
BUDGET=90          # 整段拉图的墙钟预算（秒）—— 再兜一层
POLL_USB=60        # USB/充电模式下的探测间隔（插着线时屏不刷，是设计）
MIN_SLEEP=30       # ★ v5.8：实睡短于这个秒数才算「真没睡下去」；超过则是「睡过但被打断」
WIFI_WAIT_A=6      # 第一段 WiFi 等待：6 x 5s = 30s
WIFI_WAIT_B=12     # 关/开重置后的第二段等待：12 x 5s = 60s

BAT=/sys/class/power_supply/bd71827_bat
[ -d "$BAT" ] || BAT=$(ls -d /sys/class/power_supply/*bat* 2>/dev/null | head -1)
WA=/sys/class/rtc/rtc0/wakealarm

load_conf() {
  [ -f "$CONF" ] || return 0
  . "$CONF" 2>/dev/null
  case "$POLL" in ''|*[!0-9]*) POLL=3540;; esac
  case "$KEEP" in ''|*[!0-9]*) KEEP=1;; esac
  case "$SLEEP" in 0|1) ;; *) SLEEP=1;; esac
  case "$WIFI_OFF" in 0|1) ;; *) WIFI_OFF=0;; esac
  case "$WIFI_RECOVER" in 0|1|2) ;; *) WIFI_RECOVER=0;; esac
  [ -n "$URL" ] || URL=http://YOUR_SERVER_IP/board.png
  if [ "$SLEEP" != "$_s" ] || [ "$WIFI_OFF" != "$_w" ] || [ "$WIFI_RECOVER" != "$_r" ] || [ "$POLL" != "$_p" ] || [ "$URL" != "$_u" ]; then
    echo "[loop][$(date '+%m-%d %H:%M:%S')] conf applied: SLEEP=$SLEEP WIFI_OFF=$WIFI_OFF WIFI_RECOVER=$WIFI_RECOVER POLL=$POLL KEEP=$KEEP"
    _s=$SLEEP; _w=$WIFI_OFF; _r=$WIFI_RECOVER; _p=$POLL; _k=$KEEP; _u=$URL
  fi
}

SL() {
  echo "[slp][$(date '+%m-%d %H:%M:%S')] $*" >> "$SLOG"
  if [ "$(wc -c < "$SLOG" 2>/dev/null || echo 0)" -gt 65536 ]; then
    tail -n 250 "$SLOG" > "$SLOG.t" 2>/dev/null && mv "$SLOG.t" "$SLOG"
  fi
}

hb_set() { echo "$1" > "$HB" 2>/dev/null; }
sfail_get() { cat "$SFAIL" 2>/dev/null || echo 0; }
sfail_set() { echo "$1" > "$SFAIL" 2>/dev/null; }
# 拉图连续失败计数（退避用）。成功即归零。
wf_get() { cat "$WFAIL" 2>/dev/null || echo 0; }
wf_set() { echo "$1" > "$WFAIL" 2>/dev/null; }

# WiFi 是否真的可用（以 wlan0 拿到 IPv4 为准）
wifi_up() { ifconfig wlan0 2>/dev/null | grep -q 'inet addr'; }

# ★ 确保 WiFi 连上。返回 0 = 已连上；1 = 连不上。
#   判据是「wlan0 真的有 IPv4」，不是"重试 wget 若干次" —— 应用层重试会掩盖故障层级。
#   ★ v5.7：默认(WIFI_RECOVER=0)**绝不主动关 WiFi**。
#     依据实测：关 WiFi + suspend 会让 wifid 卡在 cmState=PENDING、
#     wlan0 TX packets 恒为 0，连整机重启都救不回来。
#     ⇒ 主动关 WiFi 是风险源，不是救援手段。只做 enable 1 + 等待。
ensure_wifi() {
  if wifi_up; then return 0; fi
  lipc-set-prop com.lab126.cmd wirelessEnable 1 >/dev/null 2>&1
  i=0
  while [ "$i" -lt $WIFI_WAIT_A ]; do
    wifi_up && { SL "WiFi 已连上（等了 $((i*5))s）"; return 0; }
    i=$((i+1)); sleep 5
  done
  if [ "$WIFI_RECOVER" -ge 1 ]; then
    SL "WiFi 未在 $((WIFI_WAIT_A*5))s 内连上 -> 执行 关/开 重置（WIFI_RECOVER=$WIFI_RECOVER）"
    lipc-set-prop com.lab126.cmd wirelessEnable 0 >/dev/null 2>&1
    sleep 5
    lipc-set-prop com.lab126.cmd wirelessEnable 1 >/dev/null 2>&1
    i=0
    while [ "$i" -lt $WIFI_WAIT_B ]; do
      wifi_up && { SL "WiFi 关/开重置后连上（共等 $((WIFI_WAIT_A*5+5+i*5))s）"; return 0; }
      i=$((i+1)); sleep 5
    done
    SL "WiFi 关/开重置后仍无 IP"
  else
    SL "WiFi 未在 $((WIFI_WAIT_A*5))s 内连上（WIFI_RECOVER=0：不主动关 WiFi，避免加重 wedge）"
  fi
  return 1
}

# ★ WiFi 故障快照 —— 只用 busybox 安全命令 + 一次带超时保护的 lipc 探测，
#   目的是把"到底断在哪一层"（无链路 / 无IP / 有IP不通）变成可读证据。
wf_diag() {
  SL "  [wifi诊断] ==== 开始快照 ===="
  {
    echo "[wifi诊断] -- ifconfig wlan0 --"
    ifconfig wlan0 2>&1
    echo "[wifi诊断] -- ifconfig -a --"
    ifconfig -a 2>&1
    echo "[wifi诊断] -- route -n --"
    route -n 2>&1
    echo "[wifi诊断] -- /proc/net/wireless --"
    cat /proc/net/wireless 2>&1
  } | sed 's/^/    /' >> "$SLOG"
  # wifid 的 CM 状态最有价值，但 USB 模式下 lipc 可能阻塞 -> 加 5 秒硬超时
  rm -f "$BOARD/.wst"
  ( lipc-get-prop com.lab126.wifid cmState > "$BOARD/.wst" 2>&1 ) &
  CP=$!
  sleep 5
  kill -9 "$CP" 2>/dev/null
  SL "  [wifi诊断] wifid cmState = $(cat "$BOARD/.wst" 2>/dev/null || echo '(超时/无响应)')"
  SL "  [wifi诊断] wirelessEnable = $(lipc-get-prop com.lab126.cmd wirelessEnable 2>/dev/null || echo '?')"
  SL "  [wifi诊断] ==== 快照结束 ===="
}

ensure_watch() {
  [ -f "$WATCH" ] || return 0
  W=$(cat "$WPIDF" 2>/dev/null)
  if [ -n "$W" ] && kill -0 "$W" 2>/dev/null; then return 0; fi
  SL "看门狗不在，重新拉起"
  setsid sh "$WATCH" </dev/null >>"$BOARD/watch.out" 2>&1 &
}

# ── 电量叠加（方案 B：设备侧 eips 文字，不依赖服务端）──
BATT_X=${BATT_X:-68}
BATT_Y=${BATT_Y:-6}
read_batt() {
  b=""
  [ -n "$BAT" ] && [ -r "$BAT/capacity" ] && b=$(cat "$BAT/capacity" 2>/dev/null)
  if [ -z "$b" ]; then
    for f in /sys/class/power_supply/*/capacity; do
      [ -r "$f" ] || continue
      b=$(cat "$f" 2>/dev/null); break
    done
  fi
  case "$b" in ''|*[!0-9]*) echo "";; *) echo "$b";; esac
}
show_board() {
  eips -f -g "$CUR" >/dev/null 2>&1 || return 1
  B=$(read_batt)
  [ -n "$B" ] && eips $BATT_X $BATT_Y "${B}%" 2>/dev/null || true
  return 0
}

load_conf
echo $$ > "$PIDF"
hb_set $(( $(date +%s) + 900 ))
echo "[loop] started pid=$$ $(date '+%m-%d %H:%M:%S') SLEEP=$SLEEP WIFI_OFF=$WIFI_OFF POLL=$POLL"
SL "loop started pid=$$ SLEEP=$SLEEP WIFI_OFF=$WIFI_OFF POLL=$POLL 睡眠失败计数=$(sfail_get) 拉图失败计数=$(wf_get) 版本=v5.8"

sleep 15

force=3            # 开场连补 3 次，每次 15 秒，耐住「回图书馆」那一下的二次重绘
n=0                # 常驻模式（SLEEP=0）的 KEEP 计数
fast=3             # 头 3 轮用 FASTP 短周期自检能否睡着
woke=1             # 本轮是否刚从睡眠醒来
_usb="0"           # ★ v5.5：初始化，避免首轮误报「已脱离 USB/充电」

while true; do
  load_conf
  NOW=$(date +%s)
  hb_set $(( NOW + 900 ))
  ensure_watch

  # ---------- USB/充电门禁（纯 sysfs，绝不用可能阻塞的 lipc）----------
  USB_MODE=0
  for n in /sys/class/power_supply/*usb* /sys/class/power_supply/*ac*; do
    [ -f "$n/present" ] && [ "$(cat "$n/present" 2>/dev/null)" = "1" ] && USB_MODE=1
  done
  [ "$(cat "$BAT/status" 2>/dev/null)" = "Charging" ] && USB_MODE=1
  if [ "$USB_MODE" = "0" ]; then
    WT=$BOARD/.wtest
    (echo x > "$WT") 2>/dev/null || USB_MODE=1
    rm -f "$WT" 2>/dev/null
  fi

  if [ "$USB_MODE" != "$_usb" ]; then
    if [ "$USB_MODE" = "1" ]; then
      SL "USB/充电中 -> 整轮挂起（不碰 lipc/wget/eips），每 ${POLL_USB}s 探一次"
      SL "  USB 模式下 Kindle 的写入对电脑不可见，日志必须拔线后才读得到"
      SL "  要验证深度睡眠：拔线并保持 10 分钟以上；插着线永远不会睡"
    else
      SL "已脱离 USB/充电 -> 恢复刷新与睡眠"
    fi
    _usb=$USB_MODE
  fi

  if [ "$USB_MODE" = "1" ]; then
    hb_set $(( $(date +%s) + 900 ))
    sleep $POLL_USB
    woke=1
    continue
  fi

  # ---------- preventScreenSaver（只在非 USB 模式才碰 lipc）----------
  if [ "$SLEEP" = "1" ]; then
    lipc-set-prop com.lab126.powerd preventScreenSaver 0 >/dev/null 2>&1
  else
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 >/dev/null 2>&1
  fi

  # ---------- 本轮睡眠时长：基础值 + 拉图失败退避 ----------
  IVAL=$POLL
  [ "$fast" -gt 0 ] && IVAL=$FASTP
  WF=$(wf_get)
  case "$WF" in ''|*[!0-9]*) WF=0;; esac
  if [ "$WF" -gt 0 ]; then
    IVAL=$(( RETRY * (1 << WF) ))
    [ "$IVAL" -gt "$POLL" ] && IVAL=$POLL
  fi

  # ---------- ① 睡眠许可（闹钟不在这里设 —— 见 ⑥ 睡前精确设）----------
  #   ★ v5.6 修正：v5.2~v5.5 在轮次开头就设闹钟，而 ensure_wifi 最多烧 95s，
  #     这段时间直接从睡眠里扣掉（实测 IVAL=120 时只睡了 13s）。
  #     现在改为**睡前那一刻**才设，以"真正睡着"为基准计时。
  CAN_SLEEP=0
  if [ "$SLEEP" = "1" ] && [ -w "$WA" ] && [ "$(sfail_get)" -lt 3 ]; then
    CAN_SLEEP=1
  fi
  NE=0

  # ---------- ② WiFi（等真 IP，不盲重试）----------
  #   ★ v5.7：连续失败 >=3 轮后**跳过等待与拉图直接睡** ——
  #     长期 WiFi 故障时，每轮白等 30s 且保持 Active 是纯耗电，不如多睡。
  WOK=1
  if [ "$CAN_SLEEP" = "1" ]; then
    WFN=$(wf_get); case "$WFN" in ''|*[!0-9]*) WFN=0;; esac
    if [ "$WFN" -ge 3 ]; then
      WOK=0
      SL "WiFi 已连续失败 ${WFN} 次 -> 本轮跳过等待与拉图，直接睡（省电优先）"
    elif ensure_wifi; then
      : 
    else
      WOK=0
      SL "WARN WiFi 连不上 -> 本轮跳过拉图"
      [ "$WFN" -ge 1 ] && wf_diag
    fi
  fi

  # ---------- ③ 拉图（硬超时 + 墙钟预算）----------
  ok=0; SZ=0; i=0; T0=$(date +%s)
  if [ "$WOK" = "1" ]; then
    while [ "$i" -lt 12 ]; do
      if wget -T $WGET_TO -q -O "$NEW" "$URL" >/dev/null 2>&1; then
        SZ=$(wc -c < "$NEW" 2>/dev/null)
        if [ "${SZ:-0}" -gt 1000 ]; then ok=1; break; fi
      fi
      i=$((i+1))
      [ $(( $(date +%s) - T0 )) -ge "$BUDGET" ] && break
      sleep 5
    done
  fi
  EL=$(( $(date +%s) - T0 ))

  # ---------- ④ 刷屏 ----------
  doit=0
  if [ "$force" -gt 0 ] || [ "$woke" = "1" ] || [ ! -f "$CUR" ]; then
    doit=1
  elif [ -f "$NEW" ] && ! cmp -s "$NEW" "$CUR"; then
    doit=1
  fi

  if [ "$ok" = "1" ]; then
    wf_set 0
    if [ "$doit" = "1" ]; then
      mv "$NEW" "$CUR"
      if show_board; then
        if [ "$force" -gt 0 ]; then
          SL "warmup $force  图 ${SZ}B 上屏"
        else
          SL "refresh 图 ${SZ}B 上屏"
        fi
        [ "$force" -gt 0 ] && force=$((force-1))
        n=0
      else
        SL "ERROR eips 失败 图 ${SZ}B"
      fi
    else
      rm -f "$NEW"
      n=$((n+1))
      if [ "$CAN_SLEEP" != "1" ] && [ "$n" -ge "$KEEP" ]; then
        n=0
        show_board >/dev/null 2>&1 && SL "keep-alive redraw（常驻模式兜底）"
      fi
    fi
  else
    rm -f "$NEW"
    NF=$(( WF + 1 ))
    [ "$NF" -gt 6 ] && NF=6
    wf_set $NF
    NB=$(( RETRY * (1 << NF) ))
    [ "$NB" -gt "$POLL" ] && NB=$POLL
    SL "WARN 本轮拉图失败（wifi=${WOK} 试了 ${i} 次 / 墙钟 ${EL}s）-> 退避 ${NB}s 后重试，拉图失败计数=${NF}"
    [ "$NF" -ge 2 ] && [ "$WOK" = "1" ] && wf_diag
  fi

  # ---------- ⑤ 关 WiFi（只在拉图成功那轮关；失败轮留着让它自己关联）----------
  if [ "$WIFI_OFF" = "1" ] && [ "$ok" = "1" ] && [ "$CAN_SLEEP" = "1" ]; then
    lipc-set-prop com.lab126.cmd wirelessEnable 0 >/dev/null 2>&1
    sleep 3
  fi

  # ---------- ⑥ 睡（★ 睡眠是默认行为，失败轮也睡，只是时间短）----------
  if [ "$CAN_SLEEP" = "1" ]; then
    CAP=$(cat "$BAT/capacity" 2>/dev/null)
    CURUA=$(cat "$BAT/current_now" 2>/dev/null)
    sync
    T1=$(date +%s)
    # ★★ v5.6：闹钟在睡前这一刻才设，NE 以"即将入睡"为基准（护栏：回读失败就不睡）
    NE=$(( T1 + IVAL ))
    echo 0 > "$WA" 2>/dev/null
    echo "$NE" > "$WA" 2>/dev/null
    RB=$(cat "$WA" 2>/dev/null)
    if [ -z "$RB" ] || [ "$RB" = "0" ]; then
      SL "WARN 睡前闹钟回读为空 -> 本轮不睡（护栏1生效）"
      CAN_SLEEP=0
    fi
  fi

  if [ "$CAN_SLEEP" = "1" ]; then
    hb_set $(( NE + 900 ))
    echo mem > /sys/power/state 2>>"$SLOG"
    T2=$(date +%s)
    DR=$((T2 - T1))

    CAP2=$(cat "$BAT/capacity" 2>/dev/null)
    TH=$((IVAL * 3 / 4))
    INTD=0
    if [ "$T2" -ge "$((NE - TH))" ]; then
      # 睡满（允许提前 25%）
      sfail_set 0
      if [ "$ok" = "1" ]; then
        SL "SLEEP OK  闹钟=${IVAL}s 实睡=${DR}s  电量 ${CAP}%->${CAP2}%  睡前 ${CURUA}uA"
      else
        SL "SLEEP OK（退避轮）  闹钟=${IVAL}s 实睡=${DR}s  电量 ${CAP}%->${CAP2}%"
      fi
      [ "$fast" -gt 0 ] && [ "$ok" = "1" ] && fast=$((fast-1))
    elif [ "$DR" -ge $MIN_SLEEP ]; then
      # ★ v5.8：睡过了但被外部事件提前唤醒 —— 不是故障，不计失败。
      #   唤醒源：USB 插入（实测）、电源键（用户查看看板）、其他硬件事件。
      sfail_set 0
      INTD=1
      UM=0
      for n in /sys/class/power_supply/*usb* /sys/class/power_supply/*ac*; do
        [ -f "$n/present" ] && [ "$(cat "$n/present" 2>/dev/null)" = "1" ] && UM=1
      done
      if [ "$UM" = "1" ]; then
        SL "SLEEP 被打断（实睡 ${DR}s / 闹钟 ${IVAL}s）USB 插入 -> 不算失败，下轮按 POLL 重睡"
      else
        SL "SLEEP 被打断（实睡 ${DR}s / 闹钟 ${IVAL}s）外部唤醒（电源键等）-> 不算失败，下轮按 POLL 重睡"
      fi
    else
      # 真故障：根本没进 suspend（echo mem 被拒 / 立刻返回）
      F=$(( $(sfail_get) + 1 ))
      sfail_set $F
      if [ "$F" -ge 3 ]; then
        SL "SLEEP FAIL 没睡下去（实睡仅 ${DR}s）连续失败=$F/3 -> 已停用睡眠，退回常驻模式"
      else
        SL "SLEEP FAIL 没睡下去（实睡仅 ${DR}s）连续失败=$F/3（下轮再试）"
      fi
    fi
    woke=1

    # ★ v5.8：被外部唤醒时，先立刻把当前图重刷回屏（不等联网）
    #   唤醒后 framework 会画锁屏/图书馆盖住看板，而 ensure_wifi+wget 要 5~20s。
    if [ "$INTD" = "1" ] && [ -f "$CUR" ]; then
      if show_board; then
        SL "被打断唤醒 -> 立刻重刷当前图（不等联网，用户无需等待）"
      fi
    fi
  else
    hb_set $(( $(date +%s) + IVAL + 900 ))
    sleep $IVAL
    woke=0
  fi

  [ "$force" -gt 0 ] && sleep 15
done
EOS
chmod 755 "$LOOP" 2>/dev/null || true

echo "[board] 5) 写看门狗 $WATCH"
cat > "$WATCH" <<'EOW'
#!/bin/sh
# Kindle 看板看门狗（由 board.sh 生成）
# 职责单一：心跳过期 / 循环进程消失 -> 把循环重启回来。循环每轮也会反过来确保本脚本活着。
BOARD=/mnt/us/board
HB=$BOARD/.hb
PIDF=$BOARD/loop.pid
WLOG=$BOARD/watch.log
LOOP=$BOARD/loop.sh

WL() {
  echo "[wd][$(date '+%m-%d %H:%M:%S')] $*" >> "$WLOG"
  if [ "$(wc -c < "$WLOG" 2>/dev/null || echo 0)" -gt 32768 ]; then
    tail -n 150 "$WLOG" > "$WLOG.t" 2>/dev/null && mv "$WLOG.t" "$WLOG"
  fi
}

echo $$ > "$BOARD/watch.pid"
WL "看门狗启动 pid=$$"

sleep 30

while true; do
  NOW=$(date +%s)

  # ★ USB 模式下循环是「主动挂起」的，此时不该重启它（重启也只会再次挂起）。
  #   检测走纯 sysfs，不看 lipc（USB 模式下 lipc 可能阻塞）。
  U=0
  for n in /sys/class/power_supply/*usb* /sys/class/power_supply/*ac*; do
    [ -f "$n/present" ] && [ "$(cat "$n/present" 2>/dev/null)" = "1" ] && U=1
  done
  if [ "$U" = "1" ]; then
    sleep 240
    continue
  fi

  LP=$(cat "$PIDF" 2>/dev/null)
  DL=$(cat "$HB" 2>/dev/null)
  case "$DL" in ''|*[!0-9]*) DL=0;; esac

  BAD=""
  if [ -z "$LP" ]; then
    BAD="loop.pid 缺失"
  elif ! kill -0 "$LP" 2>/dev/null; then
    BAD="循环进程 $LP 已不在"
  elif [ "$DL" = "0" ]; then
    BAD="心跳文件缺失"
  elif [ "$NOW" -gt "$DL" ]; then
    BAD="心跳过期 $((NOW - DL))s（期限 $DL）"
  fi

  if [ -n "$BAD" ]; then
    WL "重启循环：$BAD"
    [ -n "$LP" ] && kill -9 "$LP" 2>/dev/null
    sleep 2
    if [ -f "$LOOP" ]; then
      setsid sh "$LOOP" </dev/null >>"$BOARD/loop.out" 2>&1 &
      WL "已重新拉起 loop.sh"
    else
      WL "ERROR 找不到 $LOOP"
    fi
    sleep 40
  fi
  sleep 240
done
EOW
chmod 755 "$WATCH" 2>/dev/null || true

echo "[board] 6) 启动看门狗 + 后台循环"
if command -v setsid >/dev/null 2>&1; then
  setsid sh "$WATCH" </dev/null >>"$BOARD/watch.out" 2>&1 &
  echo "[board]    看门狗 via setsid"
  setsid sh "$LOOP" </dev/null >>"$BOARD/loop.out" 2>&1 &
  echo "[board]    循环 via setsid"
else
  nohup sh "$WATCH" </dev/null >>"$BOARD/watch.out" 2>&1 &
  echo "[board]    看门狗 via nohup"
  nohup sh "$LOOP" </dev/null >>"$BOARD/loop.out" 2>&1 &
  echo "[board]    循环 via nohup"
fi

sleep 5
LP=$(cat "$PIDF" 2>/dev/null)
WP=$(cat "$WPIDF" 2>/dev/null)
echo "[board]    循环 pid=${LP:-未写出}"
echo "[board]    看门狗 pid=${WP:-未写出}"
echo "[board] 完成。v5.8：首刷约 15 秒后上屏；头 3 轮 3 分钟短周期自检睡眠。"
echo "[board]   USB 模式=整轮挂起；WiFi 不关不重置；被唤醒只算打断不算失败"
echo "[board]   诊断看 sleep.log（睡眠/退避）／ watch.log（看门狗）"
