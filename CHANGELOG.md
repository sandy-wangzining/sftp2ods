# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)；格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [Unreleased]

### 修复

- **锁目录退回时显式提示（可靠性）**：工具目录不可写、锁退回系统临时目录时原来没有任何
  提示——root 与普通用户混跑同一作业会各拿一把锁、互斥静默失效；现在退回（含兜底路径）
  会打印提示，指引用 `SFTP2ODS_LOCK_DIR` 钉住（与 feishu2ods 同款）。

- **`__warnings__` 通道只认列表（正确性）**：用户误写的同名非 list 键原来会被
  `extend("oops")` 按字符拆成假告警、且第一次收集就把键 pop 走；现在只读不 pop、
  非 list 落入"未知配置项"扫描（与 api2ods 同款）。

- **锁名计算对任意文件名都成立（可靠性）**：作业路径含非 UTF-8 字节（surrogateescape 的
  代理字符，如从旧系统解包出来的文件名）时，锁名哈希原来 `encode("utf-8")` 直接抛
  UnicodeEncodeError、进程在加锁前就崩溃；改用 `os.fsencode`（与文件系统同口径）。
  值级脱敏的"激进编码"变体对同款代理字符补 `surrogatepass`，`quote`/`quote_plus`
  编码变体遇到代理字符时跳过该形态，日志出口的脱敏再包一层兜底——脱敏本身绝不抛。

- **as_bool 拒绝数组/对象（正确性）**：原来非字符串类型走 `bool()` 兜底——`[]` 会被
  静默当成 False、绕过"未知一律报错"的 fail-closed 约定；现在容器类型直接报配置错
  （0/1 等数字仍按真值）。

- **日志写入不再持有全局锁（可靠性）**：log() 原来在模块级锁内执行 stdout 与 --log-file
  的 write/flush——慢速目标（管道被压满、NFS/满盘上的日志盘）会把其它线程的 log_once /
  add_log_sink / remove_log_sink 一起卡死，整个进程表现为停滞；现在锁内只做 sink 快照，
  写入全部在锁外（写失败摘除仍在锁内，且不覆盖并发新加进来的 sink）。

- **redact_secrets 接受裸标量 values（健壮性）**：`secrets` 直接写成数字（`secrets: 123456`）
  时，原来 `for v in 123456` 抛 TypeError、把真正的失败原因顶掉；现在与单字符串同口径按
  "只有一个密钥"处理（与 api2ods / feishu2ods 同款）。

- **值级脱敏补两个口子（安全）**：密钥值的 URL 编码形态原来只替 quote/quote_plus 两种
  （`+`/`/`/`=` 被覆盖），部分编码器把 `-` 也编码成 `%2D` 时仍会漏——补"非字母数字全编码"
  变体（按 UTF-8 **字节**判断，含中文的口令也生成正确编码形态）；`redact_secrets` 对非字符串
  密钥值（数字）原来直接 len() 抛 TypeError、把真正的失败原因顶掉，现在先 str 化
  （None/bool 跳过，避免把文本里的 "None"/"True" 误替）。

- **内联占位符解析成 null/容器时直接报错（正确性）**：`"root": "/data/${secrets.ns}/daily"`
  且 secrets.ns 为 null 时，原来 str(None) 静默拼出 "/data/None/daily"，校验全过、运行期
  去列一个不存在的目录——现在与字典键路径同口径报 ConfigError（凭据字段的内联也一样，
  不再把密码拼成 "pre-None"）；整串占位符解析成 null 同样拒绝，容器/数字仍是合法的整串结果。

- **凭据字段不再被 `${...}` 字面量卡住加载（可用性）**：密码/口令（键名含 password/passphrase 等）
  的值是自由文本，里面出现畸形或未知的 `${...}`（如 `p@ss${word`、整串就是 `${secrets.old}`
  而 secrets 里没有这个键）原来会报"占位符写法不对/引用了不存在的占位符"、整份作业加载失败；
  现在这类字段能解析的占位符照常解析、其余按字面量保留，非凭据字段的写法错误仍然报错。

- **删/建分区的 DDL 分区值也过白名单（安全）**：`_sql_spec` 原来只做引号转义，drop/add 的
  分区值没套"8 位业务日"白名单（count/write 路径有）——现在统一走 `_partition_literal`，
  同一个参数一套标准。

- **首次重试退避也受 `max_delay` 约束（可靠性）**：`base_delay` 配得比 `max_delay` 大时，
  第一次的 sleep 不受上限（只有后续退避被夹取）。

- **AK/SK 只填一半改为报错、不再静默回退（正确性）**：maxcompute 块里只写一半（典型：secret
  键名拼错成 `access_key_secect`）原来只警告然后回退到环境变量/本机 aliyun CLI——那会用
  另一个身份写库（审计/计费/归属全错），报错也指不回配置；现在与「--mc-profile 找不到不回退」
  「非映射报错」同口径直接拒绝，并给出缺失键名与两种修复方式。

- **向导两处小修（可用性）**：没有金额（decimal）列时不再写 `footer: {"sum": []}` 空配置
  （运行时按空列表跳过核对，与"没配"等价却让用户以为配了）而是明说未启用；默认表名把作业名里
  的连字符换成下划线（连字符可用于文件名、但 MaxCompute 表名不允许，原样生成会被 --check 拦下）。

- **测试基建三处（可靠性，不影响生产行为）**：`World.__enter__` 启动 mock 途中失败会回滚
  已启动的 patcher，不再把全局 mock 留在原地污染后续用例；fake 的 DDL 分支解析不了 SQL 时
  显式 AssertionError（原来静默返回空实例，用例按"生产没删分区"的误导性原因失败）；
  `test_broken_sink_is_closed_when_dropped` 摘 sink 改 try/finally（断言失败不再把坏 sink
  留进程里连锁污染）。另三处测试断言收紧：cell 超限用例补"分区未删/未开写入"断言、
  慢提交用例度量总耗时（预算被重置时最低多睡一个轮询周期）、向导写盘阶段 Ctrl+C 断言
  从 `rc ∈ {1,130}` 收紧为 130。

- **锁目录可用 `SFTP2ODS_LOCK_DIR` 固定（可靠性）**：工具目录不可写时会退回系统临时目录，
  同一作业的 root 实例与普通用户实例会锁在不同文件上、互斥静默失效；现在可用环境变量把锁目录
  钉在固定位置（指定的目录不可用直接报错、不静默换；指到共享存储时多机也能互斥）。

- **`source.layout` 的大小写不再改变行为（正确性）**：`validate_job` 按 `.lower()` 校验、`SftpSource`
  却按字面量比较，`"layout": "DATE_DIR"` 能过校验但运行时按 `flat` 列目录（结果为空，报"远端目录下
  没有任何匹配文件"，与真正的配置错指不到一起）；现在 `normalize_job` / `SftpSource` 统一归一成小写。

- **准备阶段报错也按 `--config` 里的密钥值脱敏（安全）**：原来只收作业文件（`job_raw`）里的密钥值，
  凭证只写在 `--config` 的 `secrets` / `maxcompute` 里时，报错回显的明文会漏遮；现在两份配置一起收，
  与 api2ods 同口径。

- **分区增删改走带超时的 DDL（可靠性）**：`write_partition` 原来用 pyodps 的 `delete_partition` /
  `create_partition`（同步、不限时），云端分区元数据操作卡住会把整轮任务连同运行锁一起挂死——
  而 README 写着"删分区有 `--sql-timeout` 保护"。现在改成 `alter table ... drop/add if [not] exists
  partition (pt='...')` 走 `run_sql_with_timeout`（与 feishu2ods 同口径），README 的这条承诺随之成立。

- **建表 DDL 的 `LIFECYCLE` 顺序对齐 api2ods（可用性）**：`lifecycle n` 原来排在 `tblproperties (...)`
  之前，改为之后——与官方 PK 表语法 `[TBLPROPERTIES (...)] [LIFECYCLE <days>]`、以及 api2ods
  （示例作业带 `lifecycle_days` 且实际跑过）一致。

- **两处脱敏加固（安全）**：敏感键 + 无引号的值改为遮到行尾/`&`——`password=my secret`
  原来只遮第一个词、`secret` 明文留下（口令短语很常见）；URL userinfo 的口令按"最后一个
  `@`"切分——`https://user:p@ss@proxy:8080` 原来在第一个 @ 截断、口令余段明文留下。
  已是 `***` 形态的文本不重复吞（负向先行断言），非敏感键不吞后续键（扫描器实现）；
  未闭合引号（`password="abc` 被日志截断）与「带引号的键」+ 不带引号的值
  （`"password": my secret`）不再绕过三套规则。

- **四处边角（正确性/可用性）**：`resolve_download_dir` 对非对象 source 走
  `check_block_types` 给中文配置错（原来裸 AttributeError）；`build_table_ddl` 的
  `lifecycle_days=0`（常被理解成"永不过期"）显式报错、不再真值判断静默省略（表会按
  项目默认生命周期回收数据）；`safe_job_name` 对只含合法字符的名字（含首尾下划线）
  原样保留——`recon_` 不再被 strip 成 `recon-<hash>` 改写下载目录；向导密钥类输入
  只去尾部换行不 strip（与 api2ods 同款），采样提示如实说"取样文件"（同一天多个文件
  按名称取第一个，不再自称"最新文件"）。

- **SQL 超时预算从提交前开始计（可用性）**：原来 `started` 在 `o.run_sql` 之后——
  提交本身在网关侧卡了 5 分钟，轮询还会再拿一整份新预算（总时长 = 提交 + timeout）；
  现在提交耗时计入同一预算（超了立即 stop + TimeoutError）。提交与 Tunnel 的单次 HTTP
  另由 pyodps 的 connect/read 超时兜底；Tunnel 写入的边界已在注释写明，建议调度侧给
  作业配 task 超时。

- **向导本地样本的 expanduser 移进 try、非对象 target 走守卫（可用性）**：`~user` 解析
  不了（本地无该用户 / HOME 未设置）时 `Path.expanduser` 抛的 RuntimeError 原来在 try
  之外、会以裸 traceback 终止整个向导，现在提示后重试；`get_mc_profile_meta`/
  `resolve_target` 对非对象 target（字符串/数组）改用 `_as_mapping`，给中文配置错而不是
  裸 AttributeError。

- **六处边角收紧（正确性/数据安全）**：`ensure_target_table` 改为全限定取表
  （`o.get_table("project.table")`）——连接默认 project 与 target.project 不同时会拿到
  另一个 project 的同名表，校验错对象、Tunnel 写错库；`run_sql_with_timeout` 的轮询
  遇任何异常都先 `stop()` 云端实例再抛出（原来只覆盖超时分支，报错退出后云端昂贵 SQL
  还在跑、重跑与它并发操作同一分区）；`RunLock.__enter__` 对 `_try_lock` 的所有异常
  都关闭锁文件句柄（fail-closed 抛 SystemExit 时原来按次泄漏 fd）；配置合计行时空文件
  直接中止（原来只警告、静默写 0 行——先删后填等于清空分区）；向导对
  `expanduser` 的 RuntimeError（`~user` 无法解析）提示后重试；测试 fake 的 count 语句
  分区值解析改用正则（空格/大小写容错，避免合法 SQL 触发误导性断言）。

- **log() 的 stdout/stderr 兜底扩展（可用性）**：除断管（OSError/ValueError）外再兜
  RuntimeError/AttributeError（sys.stdout 属性缺失等极端形态）。

- **向导写盘成功后的中断不再谎称「未生成任何文件」（可用性）**：os.replace 已完成、收尾阶段
  （chmod/echo）被 Ctrl+C 时，含明文密钥的文件其实已在磁盘上，原来会按取消上报；现在如实
  打印已生成的路径并返回 0。—— 与 api2ods / feishu2ods 同款修复。

- **向导收尾 chmod 失败不再报「写文件失败（原文件未改动）」（正确性）**：os.replace 已完成
  后 chmod 失败（FUSE/CIFS 等不支持 chmod 的挂载）原来会落进写文件失败的 except、返回 1，
  但新配置已在磁盘上——现在只警告"请手工 chmod 600"。

- **skip_if_empty 的列不在文件表头时显式报错（数据安全）**：原来该列取值为空 → 所有数据行
  被静默跳过、整段写 0 行（先删后填的流程下等于清空分区）；现在直接中止并说明原因。

- **profiles 支持注释键、表注释转义反斜杠（正确性）**：`profiles` 里按约定写 `"//"` 注释键
  不再让 validate_job 报"必须是对象"；表注释与列注释先转义反斜杠再转义单引号（以 "\\"
  结尾的注释会吃掉 DDL 的收尾引号）。

- **文件系统不支持运行锁时改为 fail-closed（数据安全）**：原来在 NFS/只读挂载
  （ENOLCK/ENOTSUP）上"告警一次后无锁继续"——两个实例会并发写同一作业/表
  （purge/rename 互拆、数据被静默覆盖）。现在默认直接拒绝执行并给出指引；确认无人并发
  时可用 `SFTP2ODS_ALLOW_NO_LOCK=1` 显式接受无互斥风险（此时保留告警后继续）。

- **合计行识别彻底让位于列数校验（正确性）**：「多于表头」的校验原来还排在合计行识别
  之前——合计行带尾随分隔符（`,,1000.00,` 解析出 4 列而表头 3 列）会被当数据行中止；
  现在长/短行校验都只作用于数据行。

- **合计行的和值不再套用列 decimal(p,s) 范围（正确性）**：合计是派生值，多行之和天然
  可以超出单列范围（两行 99999999.99 的合法和就超 decimal(10,2)），按数据行口径校验
  会把完全合法的文件中止；格式合法性（千分位/非 ASCII/下划线/有限数）校验保留。

- **日期参数正则统一 re.ASCII、返回值强制 ASCII 拼回（正确性）**：`norm_date` 的
  `\d` 默认匹配全角数字、`int()` 也认——放行时 `--start-date ２０２６０９２０` 会
  归一化成全角字符串，plan_dates 的字符串比较把真实日期整段滤掉，作业"成功"却一个
  文件没同步。`_DAY_ISO_RE`/`_GRACE_RE` 同步加 re.ASCII。

- **下载目录名不再因归一化互相撞车（数据安全）**：`safe_job_name` 过滤后与原名不一致时
  补名字哈希——"对账A" 与 "A" 这类归一后同名的作业不再共用下载目录互扫对方文件；
  名字本身只含 `[0-9A-Za-z_-]` 时保持原名，已有下载目录不受影响。

- **maxcompute 写错时的报错不再回显配置片段（安全）**：该块里就是 AK/SK，截 80 个字符
  也可能把密钥原文打进运行日志；现在只报类型。

- **向导 y/n 问答无法识别时重问（可用性）**：原来 `startswith("y")` 判定——答"是/有/1"
  会被静默当成"否"（合计行开关、缺文件核对被悄悄关掉）；现在认 是/否/有/没有/1/0，
  无法识别的提示后重问，连续三次才按默认值。

- **显式传空 --bizdate 改为参数错（正确性）**：`--bizdate` 的 argparse 默认值原来是空串，
  与"显式传了空值"无法区分——调度脚本 `--bizdate "$pt"` 且 `$pt` 未定义时会按"未指定"
  回退处理全部日期（每个 pt 先删再填）。现在默认值是 None，显式空值经 _check_cli_args
  报退出码 2。

- **向导中断时清理含密钥的临时文件（安全）**：写盘途中 Ctrl+C（fsync 慢盘时的高发窗口）
  原来不会被清理逻辑接住（`except Exception` 漏掉 BaseException），在 jobs/ 里留下
  含明文密钥的 `.<作业名>.json.XXXX.tmp`，而向导还提示"未生成任何文件"；现在清理覆盖
  BaseException，中断前先删临时文件。

- **向导写作业文件改为临时文件 + rename 原子替换（数据安全）**：原来 O_TRUNC 直接覆盖目标，
  打开瞬间旧配置就没了，写入失败/被 kill 时磁盘上剩 0 字节或半截 JSON（还谎报"未生成任何
  文件"）；现在同目录临时文件写完 fsync 再顶替，失败时旧配置原样保留，chmod 也不再跟随
  符号链接。

- **bizdate 为空白不再吞掉有效的 SKYNET_BIZDATE（正确性）**：`a or b` 里空白串是真值、
  直接短路——调度侧 `export bizdate=" "` 会让后面有效的 SKYNET_BIZDATE 被忽略、回退处理
  全部日期；现在按"第一个非空白值"取，两者都是空白才报错。

- **台账写入加 fsync、0 字节台账按"没有台账"继续（可用性）**：只 write_text 不 fsync 时
  断电/SIGKILL 可能留下 0 字节台账，之后每次运行都以"台账读不了"硬失败；现在写盘后 fsync
  再原子替换，空文件（崩溃形态）按没有台账继续（台账是派生数据，重跑先删再填幂等）。

- **分区值/业务日白名单按 ASCII 匹配（正确性）**：Python 的 `\d` 默认匹配 Unicode 数字——
  全角数字（文件名 `结算_２０２６０９２０.csv`，中文输入法常见）会通过白名单、写进畸形分区
  （下游按 pt='20260920' 取数得 0 行，调度却显示成功）；`re.ASCII` 后只认 ASCII 数字。

- **本地样本读表头补 UnicodeDecodeError 兜底（可用性）**：与远端样本路径同口径的防御
  （read_header 正常会转成 ConfigError，这里是双保险）。

- **配置/台账/解析的六处收紧（正确性/数据安全）**：
  `collect_warnings` 对非数组的 `parse.columns`（5/true 手误）不再裸 TypeError 崩掉，
  与 build_job_summary 同口径跳过、交给校验阶段报错；`env_bizdate` 区分"变量不存在"与
  "变量存在但值为空串"（`export bizdate=$1` 且 $1 为空）——后者原来被当成未设置、静默回退
  处理全部日期（每个 pt 先删再填），现在与 --bizdate 同标准报错（只读体检告警后继续）；
  `strict_columns` 的合计行识别移到列数校验之前（合计行本来就是人写的汇总行、列数常与
  数据行不一致，原来会被误判成"列结构变了"中止合法文件）；纯中文/符号作业名不再一律
  退化成 "job" 共用下载目录（改用名字哈希后缀，多作业互扫对方文件的隐患消除）；
  `load_mc_credentials` 对非映射的 maxcompute 配置显式报配置错（原来裸 AttributeError，
  且可能静默回退到环境变量/本机 CLI 用另一个身份写库）；台账记录带 project 而本次调用
  没传 project 时不再当成"已上传"跳过（迁移后旧项目记录会把整段日期静默跳成"没数据"）。

- **notify 的成功码判定收紧（可靠性）**：`False == 0`、`0.0 == 0` 都是真，布尔 false /
  浮点 0 的"失败"响应不能被当成成功码。

- **飞书通知不再把「没有 code 的 200 响应」当成功（可靠性）**：webhook 误填成其它接口
  （回 `{"msg": "ok"}` 这类）时原来会打印「已发送」、告警通道静默失效；现在要求显式
  `code`/`StatusCode` 为 0（仅空 `{}` 保留按 HTTP 200 判定的宽容，措辞注明依据）。

- **`key='value'` 形态的密钥不再漏进日志（安全）**：query 规则的值部分不吃引号，
  `access_token='t-xxx'`（f-string 的 `!r` 插值 / repr 输出就是这种形态）在行中会整段
  漏遮；新增「键无引号 + 值带引号」规则，命中密钥词即遮值，键不敏感时递归兜底
  （值里嵌的 `token=…` 也认）。与 api2ods / feishu2ods 同款修复。

- **webhook 脱敏正则的可选前缀限长 256（性能/可用性）**：`(?i)((?:https?://[^\s"']*?)?/hook/)`
  在「超长、无空白、又不含 /hook/」的文本上二次回溯（50KB 实测 19.7s，且 redact 在
  log() 的锁内执行，会拖住所有线程）；限长后同一输入 0.29s，正常 webhook 照常遮蔽。

- **本地文件被改坏时重新下载（正确性/数据安全）**：下载准备阶段复用本地文件原来按
  "仅比大小"判断（不带台账 md5）——内容被改坏但大小没变时会复用损坏文件、解析上传、
  还把台账 md5 覆盖成损坏文件的哈希，且永远不再从远端重下；现在与"判定已上传"同口径
  带上台账 md5，对不上就重下。

- **显式指定的 aliyun CLI profile 不再回退（正确性/安全）**：`--mc-profile` 指向不存在
  或没有 AK/SK 的 profile 时，原来会静默回退到其它 profile（用另一个身份写库）；
  现在直接报错。

- **概要打印的两处类型守卫（可用性）**：`sftp.auth` 写成字符串时 `build_job_summary`
  抛裸 AttributeError；`parse.columns` 写成非可迭代值时抛裸 TypeError——概要早于校验
  运行，统一降级成空值。

- **footer 开启时要求配置列序与文件列序一致（正确性）**：合计行按「文件首列为空」识别，
  而 `parse.footer.sum` 的"不能是第一列"校验按「配置第一列」算——列序不一致时两套口径
  打架（漏检/误报）。现在 `map_header` 直接要求两者重合并给出可操作的报错。

- **`retry_call` 的确定性错误分支补脱敏（安全）**：TypeError/KeyError 等分支的消息原来
  未经值级脱敏（KeyError 回显的键里可能带凭证值），与同一函数另外两个出口口径不一致。

- **`_redact_shapes` 的 JSON 递归路径透传深度（安全）**：`_json` 回调里的 `redact()`
  漏传 `_depth+1`，深度护栏在这条路径上失效。

- **日志文件写失败的 stderr 告警过脱敏（安全）**：与其它出口同口径，异常文本里可能
  带着密钥值。

- **`raise exc` 改裸 `raise`（可用性）**：`_handle_lock_oserror` 重新抛出会重置
  traceback，排查时看不到原始栈。

- **`source.layout` / `sftp.auth.type` 的 null 归一化（正确性）**：`setdefault` 对已存在
  的 JSON null 不生效，与本文件"null/空串当未配置"的口径矛盾；改用 `_fill_default`。

- **`list_files` 按日期升序返回（可用性）**：docstring 承诺的顺序此前是 READDIR 的
  任意插入顺序；现在显式排序（下游 `max()`/遍历更稳定可读）。

- **`sftp.port` 非法值给配置错并校验范围（可用性）**：`"abc"` 原来是裸 `ValueError`，
  70000 这类越界值也会一路带到连接层；现在 1~65535 之外直接报配置错。

- **`_check_cli_args` 的空异常消息兜底（可用性）**：`SystemExit()`（无消息）时返回空串
  会被 main 当成"校验通过"继续执行；现在返回一句兜底文案。

- **下载失败分支只接预期异常（可用性）**：`except Exception` 全捕获会把 TypeError/
  AttributeError 这类代码缺陷掩盖成"下载失败"（数据问题）；现在收窄为
  (ConfigError, OSError, RuntimeError)，编程错误继续上抛。

- **超长单元格校验在重试间记忆化（性能）**：校验成功后重试不再把源数据全量重扫一遍
  （3 次重试 = 3 倍 IO/解析开销）；首趟校验失败仍随重试重来、且一定发生在删分区之前。

- **软链日期目录跟随失败留日志（可用性）**：原来静默跳过，整个业务日期无声消失；
  现在打一条警告。

- **数值列的脏值检查覆盖 decimal（正确性）**：原检查用了 `col.kind != "dec"` 排除金额列，
  但 `Decimal` 的字符串解析同样宽松——实测 `Decimal("1_0") == 10`、`Decimal("１２３") == 123`，
  金额列里的下划线/全角数字会被静默改值读进来。现在三种数值列统一拒收（两类原因分开报错）。

- **合计行的数字口径与数据行完全一致（正确性）**：合计行原来只做 `strip_thousands`，缺
  "非 ASCII/下划线"检查与 `decimal(p,s)` 范围校验——同一份文件两条路径两种口径，会出现
  "数据行拒了、合计行收了"的假不一致。现在合计行同走三项检查。

- **`collect_warnings` 的告警过值级脱敏（安全）**：此时 job 已渲染、明文密钥就在里面，
  与其它来自 job 的日志同口径走 `_redact_job`，防止拼错键名的告警文本带上字段取值。

- **`--log-file` 参数错误按「参数问题」报退出码 2（可用性）**：指向目录/打不开原来混进 1
  （运行失败），调度侧会按"数据故障"告警；现在与 README 的退出码约定一致。

- **`interprocess_lock` 与 `RunLock` 同一套加固（安全）**：POSIX 上 `O_NOFOLLOW`（锁路径是
  符号链接就拒绝跟随）、新建按 0600、显式 encoding/errors（locale 非 UTF-8 时不再读写失败）。

- **脱敏加上递归深度上限（安全）**：`_percent_encoded_secret` 解码后回调 `redact`，多层
  `%25` 编码嵌套（每层只减 3 个字符）实测 1200 层即可打爆递归栈；现在深度超过 10 层按
  "宁可多脱敏"整段遮掉。

- **占位符替换后的键要能当 JSON 键（可用性）**：整串占位符解析成列表等非字符串时报配置错
  （原来 `str()` 静默产出 `"['a', 'b']"`）；替换后键名撞车（"a" 与 `${secrets.b}` 解析成
  同一个键）也报配置错，不再静默覆盖丢掉前一个配置项。

- **向导的输入健壮性（可用性）**：下载样本块的异常收窄为预期类型（TypeError/AttributeError
  等代码缺陷继续上抛）；`isdigit()` 之外补 `isascii()`（上标数字过 isdigit 但 int() 抛错，
  原来会让向导裸崩）；端口补 1~65535 上界校验；本地样本重试用例的答案源补默认值。

- **MaxCompute 侧三处加固（可用性）**：SQL 实例"已终止但未成功"时显式报错（不再依赖
  `wait_for_success` 一定抛错）；超时 `stop()` 失败留一条日志（云端可能仍有悬挂 SQL）；
  作业里 AK/SK 只填一半时给警告（与 api2ods/feishu2ods 同口径，不再静默换成本机身份）。

- **台账与本地检查的两处 TOCTOU（可用性）**：`local_ready` 用一次 `stat` 完成"存在 + 类型 +
  大小"判断（文件被并发清理时不再抛裸 `FileNotFoundError`）；`record_of` 的 size 比较做
  None/字符串归一化，与 `local_ready` 的"大小未知"语义对齐（旧台账 size 写成字符串也能命中）。

- **`retry_call` 不再重试确定性错误（可用性）**：TypeError/AttributeError/KeyError 等编程
  错误立即报错，不再退避 5 次白等几分钟、也不把原始错误类型包成 RuntimeError。

- **`log()` 兜住 stdout 断管（可用性）**：`BrokenPipeError`（`| head` 提前退出等）时日志
  函数自身不再抛异常打断业务。

- **概要打印的块类型守卫（可用性）**：`build_job_summary` 早于配置校验，块写成字符串时
  原来抛 `'str' object has no attribute 'get'` 裸 traceback；现在统一按 `_as_mapping`
  退化成空，与未知键扫描同口径。

- **`missing.check` 显式 null 不再被当成「关」（正确性）**：`setdefault` 处理不了已存在的
  null，概要显示"关"而校验路径按默认 True 处理，两处结论相反；现在 parse/target/missing/
  notify 四个块的默认值统一走 `_fill_default`（null/空串都当未配置），概要也用 `as_bool`。

- **date_dir 布局支持指向日期目录的软链（可用性）**：flat 布局显式支持软链，date_dir 下
  原来直接跳过符号链接条目——软链的日期目录会让整个业务日期从结果里静默消失；现在跟随
  `stat` 判断后照常扫描。

- **合计行按「文件首列」识别，配置列顺序与文件不一致时不再判错列**：原来取的是"配置里第一列
  在文件中的位置"（`header_pos[0]`），而模块约定是"文件首列为空 = 合计行"；列名映射不要求
  配置顺序与文件一致，顺序不同时会把合计行当数据行入库（下游求和翻倍）、或把数据行误判成
  合计行。现在直接按文件第 0 列判断。

- **decimal 超大指数不再构造巨型整数（可用性/健壮性）**：`check_decimal_range` 原来直接算
  `10 ** (frac_digits - col.scale)`，而 frac_digits 来自单元格文本的 Decimal 指数——文件里一个
  `1e-1000000000` 就能构造 4 亿位的大整数（数百 MB 内存 + 秒级 CPU，再大直接 MemoryError，
  还不是可读的 ValueRangeError）。现在先比位数（不够就必定不能整除），只有位数足够时才取模。

- **分区值白名单覆盖写入路径（安全）**：`write_partition` 原来直接用 f-string 拼分区 spec，
  未过 `_partition_literal` 校验（`count_partition` 却校验）——同一个参数两套标准，非法值可能
  先拼进 delete 的 DDL。现在写入路径同口径校验。

- **拼进语句前的数值解析口径对齐（正确性）**：`int()`/`float()` 的字符串解析比 `Decimal` 宽松
  ——`int("1_0") == 10`（PEP 515 下划线）、`int("１２３") == 123`（Unicode 数字）都会被静默
  接受，而同一段文本在 decimal 列会报错。现在 bigint/double 列同样拒绝下划线与非 ASCII 数字。

- **日志文件写失败时句柄会被关闭（资源）**：写坏的 sink 原来只从列表里摘掉、不关句柄，
  句柄一直挂到进程退出。

- **值级脱敏传单个字符串时不再静默失效（安全）**：`redact_secrets(values, text)` 收到字符串时
  会被 `set()` 拆成单字符（全部短于最小长度被跳过）——值级脱敏静默关闭、凭证反而明文进日志。
  现在按"只有一个密钥"处理。

- **未闭合占位符的报错不再回显未脱敏的配置值（安全）**：报错文本会进日志，而配置值本身可能
  就是密钥（密码里带 `${` 这类字符时，原样回显等于把密码写进日志）；现在回显前过 `redact`。

- **`sftp.auth` 写成字符串时给配置错而非裸 ValueError（可用性）**：`dict("password")` 的
  `dictionary update sequence element #0 ...` 报错毫无指引；现在在 `normalize_job` 里先做类型
  守卫，与其它块同口径。

- **向导把 stdin 关闭与内部错误区分开（可用性）**：输入包装层把 `ValueError`/`RuntimeError`
  （stdin 关闭）翻译成 `EOFError`；向导顶层只认 `EOFError` 为"已取消"——原来连任意
  `ValueError` 一起吞，向导内部真正的错误会被误报成"已取消，未生成任何文件"。cli 侧对
  向导的未预期异常也补了一条真实错误日志（退出码仍是 1）。

- **远端样本读表头失败与本地样本同口径（可用性）**：`_read_remote_sample` 的
  `read_header` 未做异常保护，一个编码/格式异常的远端样本会终止整个向导；现在提示后返回
  None，让"三次重选样本来源"的机制生效。

- **核对区间写错给配置错（可用性）**：`find_missing` 的 `strptime` 原来抛裸 `ValueError`
  （库调用方可能绕过 CLI 校验）；现在统一报"核对区间必须是 yyyyMMdd"。

- **朴素 datetime 不再按本机时区解释（正确性）**：`expected_latest(now=...)` 收到无 tzinfo 的
  时间时原来走 `astimezone()`（按运行机器的本地时区解释，换台机器结果就变）；现在按调用方
  给的 tz 解释。

- **运行锁加固（安全/可用性）**：锁名哈希从 `sha1[:8]`（32 位）换成 `sha256[:16]`（不同作业
  碰撞后互相阻塞的概率大幅下降）；POSIX 上 `O_NOFOLLOW` + 0600（锁路径是符号链接时拒绝跟随）；
  探测文件 unlink 失败不再把整个目录判成不可用（否则会静默换目录、互斥失效）；Windows
  `msvcrt.LK_LOCK` 只等约 10 秒的语义写进 docstring。

- **SFTP/SSH 关闭失败留一条日志（可用性）**：会话清理失败原来看不到任何线索（连接泄漏/协议
  错误无从察觉）；现在打一条警告，业务结果不受影响。

- **台账行数按本地落地文件记录，台账键与远端文件名不同口径时不再 KeyError**：行数统计原来按本地
  路径名累积、写台账时却按远端 `item.name` 取值——两把键不一致时数据已写入 MaxCompute、台账却写
  不进去，异常还会穿透 `run_sync` 变成裸 traceback；同一天两个文件重名时统计还会互相覆盖、行数
  偏小并误报"写后校验不一致"。现在行数与 `local_paths` 按位置一一对应，两个坑一起消掉。
- **显式补数区间优先于环境变量 bizdate**：调度环境（DataWorks）里 `bizdate` 环境变量总是存在，
  以前执行 `--start-date/--end-date` 补数会被 `run_sync` 的"单日 vs 区间"互斥拦下、退出码还错成
  1（文档约定 2=参数问题）。现在命令行显式给了区间就忽略环境变量 bizdate；`--bizdate` 与区间
  同时显式给出仍在参数校验阶段以退出码 2 拒绝（互斥规则本身不变）。
- **合计行金额与数据行走同一套数字口径**：合计行原来用 `raw.replace(",", "")` 解析，绕过了数据行
  的千分位校验——欧式小数（`1.234,56`）、畸形千分位（`1,23`）会被静默读成错值，只在"合计不一致"
  处报出指向不明的错误。现在同样走 `strip_thousands`，违规写法给出与数据行一致的报错。
- **decimal 尾部补零不再被误判超限**：`decimal(10,2)` 下的 `1.500` 数值上精确可表示，原来按
  "小数位 3 位 > 2 位"整文件中止（文件完全正常却天天失败）；现在只有"去掉多余小数位会改值"
  （如 `1.234`）才拒绝。
- **日期参数（`norm_date`）改用结构化白名单**：原来"删掉所有 `-` 和 `/`"会把 `20-2609-21`、
  `2026092-1` 这类明显写错的日期静默归一化成合法值、落到错误的 pt 上（同一参数的 `--bizdate`
  路径是严格正则，两套标准）。现在只接受 `YYYYMMDD` 或分隔符一致的 `YYYY-MM-DD` / `YYYY/MM/DD`。
- **远端大小未知时不再误报"大小不一致"**：READDIR 未返回 `st_size`、或软链 `stat()` 失败时，
  原来把 0/链接长度当"远端大小"，下载成功后仍被判不一致且报错指向错误方向；现在标记为大小未知
  （日志显示"大小未知"），下载后跳过大小核对，`state.local_ready` 同步支持 `size=None`。
- **`file_regex` / `date_dir_regex` 必须含 `(?P<date>)` 命名组**：缺组时原来在扫描时抛
  IndexError（报错不可读、还会被重试循环当瞬时错误白重试）；现在在 `SftpSource` 构造时给出配置错。
- **向导列名去重考虑生成的后缀**：`["Amount","Amount","Amount_1"]` 原会生成两个 `amount_1`
  （重名列，生成的配置直接被校验拒绝）；现在补后缀后继续探测直到无冲突。
- **向导读本地样本失败可重试**：读不存在的文件/无权限抛的 OSError 原来穿透到最外层、被误报成
  "写文件失败"并终止；现在与解析失败一样提示后重试（"3 次机会"名实相符）。写文件失败单独收口，
  只对写入段报"写文件失败"。
- **向导只对预期异常降级为"连接失败"**：原来 `except (ConfigError, Exception)` 把代码缺陷
  （TypeError/AttributeError）也降级成"连接失败"并继续生成占位列——会产出一份列定义完全错误的
  配置却提示成功；现在只接连接类错误，其它异常继续上抛。
- **`--init` 分支补齐异常出口**：向导里的配置错（SystemExit）与 Ctrl+C 原来直接冒泡成裸
  traceback；现在与其它分支同口径（记日志后返回 1 / 130）。
- **`lifecycle_days` 的 NaN/Infinity 给出配置错**：json.load 默认接受这些字面量，原来
  `float(raw) != int(raw)` 会对 NaN 抛未捕获的 ValueError（裸 traceback）；现在统一为带字段名的
  配置错误（config 校验与 cli 兜底两处）。
- **`job_tz_of` 对非对象 `missing` 给中文报错**（原来在 `.get` 处抛裸 AttributeError）；
  **`build_job_summary` 对非对象列定义加类型守卫**（概要打印比配置校验更早，原来会崩在 AttributeError）。
- **空 `parse.columns` 在 `ParseSpec` 构造时快速失败**（原来崩在无上下文的 IndexError）。
- **`retry_call` 校验 `attempts>=1`**（原来 attempts<=0 会报"重试 -1 次仍失败：None"），重试日志
  口径统一为"第 x/n 次尝试失败"（分子分母都按总尝试次数）。
- **台账文件含非法 UTF-8 字节时给明确报错**（UnicodeDecodeError 原来逃出 except、抛裸 traceback）。
- **飞书告警对非对象 JSON 响应按失败处理**（原来 `data.get` 抛 AttributeError 打断主流程）。
- **探测分隔符失败留一条告警**（原来静默回退逗号，后续解析报错指向不明）。
- **台账改为"进程间锁 + 读盘合并"写入**：两个工具可能共用一份 `.uploaded.json`，只做原子替换
  挡不住丢更新（A 读完后 B 写入的记录会被 A 整文件覆盖）。现在 load-modify-save 外包一层
  跨平台文件锁（flock / msvcrt），持锁期间重新读盘再合并本次记录；临时文件名带 pid+uuid，
  写完 `os.replace` 原子替换。
- **第一趟解析结果可复用，小文件写库不再二次扫描**：源文件总大小 ≤ `REUSE_ROWS_MAX_BYTES`
  （32MB）时把解析好的行缓存给写库复用（`iter_batches(prepared_rows=...)`），大文件仍按原
  方式重扫，峰值内存有硬上限。
- **多文件解析：新增列统计累计而不是互相覆盖**（原来后一个文件的 `extra_headers` 会覆盖前一个）。
- **合计行（parse.footer）三处口径修正**：① 配置了合计行但文件里找不到合计行 → 直接报错
  （原来静默接受被截断的文件）；② 合计值按"表头映射"取列（文件多列/列序不同时不再取错位）；
  ③ `skip_if_empty` 跳过的数据行仍计入合计（与源文件合计行同口径，不再假报"合计不一致"）。
- **JSON 配置 null/空串不再绕过默认值**：`sftp.port/connect_timeout/io_timeout/retry_times/
  retry_delay` 原来用 `setdefault`，JSON `null` 会带着 None 进连接层；现在空串/null 一律按
  未配置补默认值。
- **非对象配置块的告警不再崩/不再逐字符噪音**：`collect_warnings` 对 sftp/sftp.auth/source/
  target/missing/notify/parse 非对象时按"空块"处理（原来 AttributeError 或把字符串拆成
  逐字符假告警）。
- **文件锁的错误分类**：busy（别人持锁）→ 按"已有任务在运行"退出；`ENOLCK/ENOTSUP`（文件
  系统不支持锁）→ 告警后不加锁继续；其它 OSError 原样抛出（原来任何 OSError 都被当成
  "锁被占用"，在不支持 flock 的文件系统上会永久挡住作业）。
- **`--log-file` 写入失败不再完全静默**：失败时向 stderr 告警一次并摘掉该 sink（主流程不受影响）。
- **显式配置的 0 不再被 `or 默认值` 吞掉**：`retry_times/retry_delay/connect_timeout/io_timeout`
  改 `_cfg_or_default`（None/空串才用默认值；0 = 不重试/零等待）。
- **`source.root="/"` 不再被 rstrip 成空后扫家目录**：新增 `_join_root`，两种布局的远端路径
  拼接统一走它。
- **主机指纹类错误改为不可重试（FatalSourceError）**：指纹不匹配（BadHostKeyException）、
  严格模式下"未知主机"原来按瞬时故障重试若干轮（白等）；现在直接失败并给排查指引；
  严格模式额外加载 `~/.ssh/known_hosts`（paramiko 的 load_system_host_keys(None) 语义随版本
  有差异，显式加载用户文件兜底）。
- **脱敏的查询串/头行规则改为迭代扫描**：原来按段递归有 O(n²)/RecursionError 风险，
  改从左到右单遍替换（嵌套 key=value 仍覆盖）。
- **`--init` 的密钥输入回退可见**：getpass 不可用退回 input() 时明确提示会明文回显；
  只捕获 EOFError/OSError（+ GetPassWarning），其它异常不再被静默降级。

### 安全

- **拼进 count SQL 的分区值加白名单**：MaxCompute 没有绑定参数，pt 只能拼进语句；现在
  `count_partition` 对分区值做 `\d{8}` 白名单校验（pt 恒为 8 位业务日）后再拼接，注入面归零。
- **默认 MaxCompute endpoint 改 https**：作业未写 endpoint 时走明文 HTTP 会暴露 AK/SK 签名与
  查询结果；`mc.DEFAULT_ENDPOINT`、向导默认值与 `jobs/*.example.json` 模板统一改为 https。
- **`require_identifier` 拒绝非字符串**：`str(None)` / `str(True)` 都能过标识符正则，配置漏填会被
  静默拼出名为 `None` / `True` 的表名；现在非字符串/空值直接报配置错。
- **`_URL_AUTH_RE` 的 scheme 部分限长（防回溯）**：无上限时在长小写字母数字串上会在每个起始位置
  贪婪回扫（实测 20KB 要 10 秒、40KB 要 50 秒）；限长后保持线性（真实 scheme 远短于 63 字符）。
- **向导写文件先按 0600 创建再写入**：文件含 SFTP 密码/AK-SK/webhook 明文，原来 `write_text` 先按
  默认 umask（通常 0644）创建、再 chmod，存在同机其他用户可读的窗口期。
- **SFTP 重试的日志与异常做值级脱敏**：带上本连接的密码/口令，遮住 paramiko 自由文本里回显的凭证。
- **`run_sql_with_timeout` 改用单调时钟**：墙钟被 NTP 校时/夏令时回拨会让超时提前触发或永不触发。

### 工程

- **测试替身按真实语义校准**：`FakeWriter` 按 pyodps 会话语义建模 reopen（失败会话残留的块在
  reopen=False 时会与本轮一起提交——生产代码漏传 `reopen=True` 的"数据翻倍"由此可被测试发现）；
  `FakeInstance.stop()` 同时置"已终止"（否则 stop 后轮询的代码在测试里死循环，且 sleep 被替换成
  空操作不会被超时打断）；`FakeOdps.run_sql` 解析不出分区值时显式 AssertionError（不再静默按 0 行
  伪装成"数据没写进去"）；删除从未被消费的 `FakeOdps.fail_writes`；`FakeSftp` 深拷贝入参 tree、
  `stat()` 对已登记目录返回目录条目（与 paramiko 语义一致）。
- **性能回归用例改相对判据**：原用 1s/2s 墙钟阈值，共享 CI 负载高时会偶发假失败；现在按"规模放大
  后耗时不应超线性暴涨"判断（含地板值兜住计时噪声），回溯爆炸/平方级退化仍然拦得住。
- **用例隔离与可移植性**：环境变量用例统一 `clear=True`；`/tmp`、`Path.home()`、Windows-only skip
  等依赖运行环境的断言改为临时目录/注入环境变量/显式判定（盘符相对名的安全分支不再被平台跳过）；
  用例间共享的列定义改深拷贝；Decimal 断言改按 `as_tuple`（能发现尾随零丢失）、double 列改
  `assertAlmostEqual`；`RunLock` 用例改用 `with`（不手工调 `__enter__`）；`warn` 模式补告警断言；
  测试文件不再把仓库根/tests 目录插到 `sys.path` 最前（改为缺失时追加，避免遮蔽同名模块）。
- 新增回归用例覆盖上述修复（台账键口径、区间优先于环境变量、合计行数字口径、decimal 尾零、
  分区值白名单、分隔符探测告警、软链/未知大小、向导异常路径、`--init` 退出码、lifecycle NaN 等）。
- 第二次复审批次的回归用例：台账并发合并写入、解析结果复用（`prepared_rows` 不再读文件）、
  合计行缺失/按表头映射/跳过行计入、零值超时与重试保留、`root="/"`、指纹不匹配不可重试、
  多文件新增列累计、Decimal 尾零断言改 `as_tuple`、配置工厂未知 override 报错、环境变量
  `clear` 用法收敛等（离线用例 334 → 363）。

### 文档

- README：`maxcompute.endpoint` 默认值标注为 https；离线用例数 303 → 334。

## [1.4.1] - 2026-10-01

### 修复

- **显式补数区间在远端没有交集时不再静默成功**：给了 `--start-date`/`--end-date`、而该区间在远端
  一个匹配文件都没有时，以前只发一条"缺文件"告警 + "本次没有要处理的日期"就 `rc=0`（看起来像
  补数成功），与"宁可失败不可静默丢数"的红线冲突。现在直接失败并 `rc=1`，提示远端数据范围、
  确认要空跑可加 `--force`。与 1.4.0「显式单日缺文件 → 失败」同口径。
  - **不受影响**：①"核对区间为空（下界晚于上界）"的既有失败分支；②不传区间（处理远端全部日期）
    时缺文件仍只告警、`rc=0`；③`missing.check: false` 不做核对、行为不变；④区间有部分交集 →
    照常同步有交集的日期；⑤区间内文件都在、只是台账已上传 → `proc_dates` 仍非空，不会误判。
- **本地落地路径必须落在下载目录内**：`_safe_remote_name` 已挡 `/`、`\`、`..`，但 Windows 上
  `Z:xxx.csv` 这类盘符相对名会让 `download_dir / 键` 跳出目录（写到别的盘）。现在拼出本地路径后
  用 `resolve()` 做包含性判断，越界即拒绝（主下载路径与 `--init` 拉样本路径都过同一道校验）。
  **不禁冒号**——远端是 POSIX，`report:20260920.csv` 这样的文件名合法，仍正常放行。

### 安全

- **值级脱敏同时覆盖 URL 编码形态的凭证**：值级替换原来只做明文 `str.replace`。paramiko 等
  把凭证写进**没有可识别键名**的自由文本报错时（如 `t%2Dabc123...` 对应 `t-abc123...`），
  形态规则挡不住，编码后的凭证会原样进日志。现在除明文外同时替换其 `quote` / `quote_plus`
  形态（长值优先的排序不变）。
- **主机指纹"严格校验"改为显式写死**：非 `auto_accept` 分支原来只调 `ssh.load_system_host_keys()`，
  "默认严格"依赖 paramiko 当前的隐式默认策略——将来 paramiko 改默认值/重构时会**静默降级为
  不校验主机指纹**（中间人风险）。现在显式 `set_missing_host_key_policy(paramiko.RejectPolicy())`，
  把意图写进代码；`host_key: "auto_accept"` 的显式降级行为不变（仍用 `AutoAddPolicy`）。
- **向导（`--init`）的密钥类输入改为不回显**：密码 / 私钥口令 / 飞书 webhook / AccessKeySecret
  原来走 `input()`，会明文回显在终端（进 scrollback、被 `script` 录制或录屏抄走）。现在走
  `getpass` 不回显；主机名、远端目录、表名等普通输入仍走 `input()`。无 tty（CI/重定向）时
  自动退回普通输入，向导照常可用。

### 工程

- **dev 依赖的 ruff 从 `==0.16.8` 升到 `==0.16.9`**，与 api2ods / feishu2ods 统一到同一版本
  （三个仓库都要跑 `ruff format --check .`，版本不一致会出现"本地干净、另一个仓库 CI 失败"）。
- 单测不再往仓库根的 `.run-locks/` 写运行锁：测试基类把锁根目录重定向到临时目录并在收尾
  清理（临时作业路径每次哈希都不同，原来在服务器上已积了几十个锁文件）。生产行为完全不变。

### 文档

- 术语统一：README / docstring / 向导提问与体检概要里的「缺文件检查」统一为「缺文件核对」
  （README 原本即用此词，代码侧的叫法与之对齐）。
- README 修正与补充：离线用例数（239 → 303）、`--init` 密钥类输入不回显、`skip_if_empty`
  的首列限制仅在使用 `footer` 时生效、值级脱敏同时覆盖 URL 编码形态。
- `jobs/*.example.json` 的 MaxCompute 项目名改为明确占位符 `my_project`（去掉内部项目名痕迹）。
  主机名遵循"通用模板用 `example.com`、厂商专用示例用该厂商公开端点"：`_template_*.example.json`
  写 `sftp.example.com`，而 `clink_settlement` / `waffo_settlement` 两个示例仍用各自厂商的 SFTP
  端点——与 README「示例与旧脚本完全同表、同口径」的叙述一致，也与 api2ods 示例保留真实厂商
  端点的口径统一。**凭据、账号名、口令一律为占位符**（表名保持与旧脚本一致）。

## [1.4.0] - 2026-10-01

### 变更（行为调整）

- **显式点名单日却整天无文件时改为失败**：调度传 `--bizdate`（或环境变量 `bizdate`）指定
  某一天、而远端那一天一个匹配文件都没有时，以前只打一条告警就 `rc=0`，`pt=<业务日>`
  分区根本不存在——飞书告警一旦被忽略就是**静默缺数**，与"宁可失败不可静默丢数"的红线冲突。
  - **原来**：`missing=[bizdate]`、`proc_dates=[]` → 只告警、`rc=0`（调度看着像成功）。
  - **现在**：明确报错并 `rc=1`，提示"该日远端无文件；若源方当天确实不产数，请用
    `missing.check: false` 跳过核对，或加 `--force` 明确继续"。
  - **不受影响**（1.2.0 的有意设计保持原样）：① 不传业务日、处理"远端全部日期"时，缺文件
    仍只告警不失败；② `--start-date/--end-date` 区间补数时，区间内缺失日期仍只告警、照常
    同步已有文件；③ `missing.check: false` 时不做核对，行为不变。
  - 按仓库既有习惯（1.2.0 亦是"行为调整"发 minor）发 **1.4.0**。

### 工程

- 补 sftp2ods 同步主路径（`run_sync`）的 Ctrl+C 契约单测：此前只有向导 `--init` 路径验证过
  退出码 130，`run_sync` 无用例（api2ods / feishu2ods 均已逐码验证 130，sftp2ods 是缺口）。
  新增用例覆盖：下载阶段中断 → `rc=130` 且**没有任何写库动作**（数据/台账均未落）、解析阶段
  中断 → `rc=130` 且不写库、写入阶段中断 → `rc=130` 并把现有真实行为钉死（分区已删、可能
  留下空/半截分区，重跑幂等自愈，不改实现）。
- 上文"显式单日缺文件失败"的回归单测：① `--bizdate` 单日无文件 → `rc=1` 且报错含该日期；
  ② 不传 `--bizdate` 存在缺失日期 → `rc=0`；③ 区间补数有缺失 → `rc=0`；`--force` 与
  `missing.check: false` 的放行行为亦有覆盖。

## [1.3.0] - 2026-09-30

### 安全

- **SFTP 主机指纹默认严格校验**（防中间人）：只认 `~/.ssh/known_hosts` 里记录过的主机；
  未知主机报错并提示 `ssh-keyscan -p <port> <host> >> ~/.ssh/known_hosts` 登记。
  新增 `sftp.host_key: "auto_accept"` 显式降级为旧行为（不校验，等价
  `StrictHostKeyChecking=no`）。已把 clink/waffo 两个源的主机指纹登记进
  wangzining 与 work 两个用户的 known_hosts，真实 --check 验证严格模式连接正常。

### 修复

- **台账新增 md5 字段**：写入成功后把本地文件的 md5 记进台账，下次运行的跳过判断
  在"大小一致"之外再校验内容——本地文件被误改/损坏但大小没变时不再被静默跳过
  （旧逻辑只比大小，会误判"已上传"、分区永远缺这份数据）。旧台账没有 md5 字段时
  退回只比大小，兼容迁移前的记录。

## [1.2.0] - 2026-09-30

### 变更（行为调整）

- **缺文件只告警、不再失败退出**：源方产数是人/上游系统排期，节假日、结算方停产出
  都是常态，缺几天不等于任务失败——现在跳过缺失日期、照常同步已有文件（rc=0），
  并发一条飞书提醒（标题「文件缺失（已跳过继续）」）。缺失日期补产出后，下次运行
  会自动下载写分区，无需人工干预。
- 真异常仍按失败处理（不会静默）：远端目录一个文件都没有、日期范围与远端没有交集、
  连接/下载/解析失败都保持 rc=1。
- README / 退出码表 / 模块 docstring 同步。

## [1.1.0] - 2026-09-30

### 功能

- **源文件表头新增列：不报错 + 飞书提醒**。源文件出现未配置的新增列时，忽略这些列的值、
  其余数据照常入库（退出码仍为 0），并发一条飞书卡片提醒（列名、文件名、加列/补跑步骤）；
  同一批新列名每次运行只提醒一次。缺必需列仍按原逻辑报错（防改名/删列写错位）。

### 工程

- `ParseSpec.map_header` 新增 `stats` 参数记录 `extra_headers`（表头校验行为不变，默认不传则忽略）；
  CLI 侧聚合后发提醒。

## [1.0.1] - 2026-09-24

复审加固：修掉几个「退出码 0 的静默错误」，并把退出码与解析口径和 README 对齐。

### 修复

- **date_dir 布局不再复用别的日期的平铺文件**：`download_dir` 里遗留的旧脚本平铺文件以前
  会被当成「本日已下载的文件」（只比字节数），迁移/换源时可能把别的日期的数据静默写进当天
  分区；现在回退按文件名查找只用于「已上传」跳过判定（台账里该键的 pt 必须匹配），
  下载与写库一律用 `下载目录/日期/文件名`。
- **列目录的瞬时失败不再被吞成「空目录 / 这天没有文件」**：`_scan` 只把「路径不存在」
  （ENOENT/ENOTDIR）当空目录；网络抖动、权限不足等错误抛出去交给重试（此前 README 承诺的
  自动重试实际失效，date_dir 下还会静默少处理一天数据）。
- **补数区间与远端没有交集时直接失败**：`--start-date/--end-date` 落在数据范围之外
  （早于远端最早）以前会静默 rc=0，看起来像补数成功。
- **解析出 0 行时先查该分区现有行数**：非 0 直接失败（`--force` 才写空分区），避免源文件被
  截断成只剩表头、或整列被 `skip_if_empty` 滤光时把已有数据清空。
- **脱敏正则的 O(n²) 退化**：`_URL_AUTH_RE` 在长的小写字母数字串（十六进制转储等）上会在每个
  起始位置贪婪回扫（实测 20KB 要 10 秒、40KB 要 54 秒，且 Ctrl+C 打断不了）；改成按 `"://"`
  与 `"@"` 预判跳过，脱敏结果不变。
- **`parse.footer` 与 `parse.skip_if_empty` 同时指向第一列**在配置阶段报错：首列为空的行会先
  被判成合计行，跳过规则永远不生效。
- 退出码对齐 README：命令行参数写错（日期格式、`--bizdate` 与区间互斥、负数 `--sql-timeout`）
  返回 2 而不是 1；`--init` 的 Ctrl+C 返回 130。
- `io_timeout` 设置失败（拿不到 SFTP 通道）时打一条警告，不再静默失效。
- **远端文件名/目录名含路径分隔符或 `..` 时拒绝**：这类名字会被拼进本地路径，
  `download_dir / "../../x.csv"` 由内核解析后能把远端内容写到下载目录之外
  （本工具不校验 host key，恶意/被劫持的服务端即可触发）。
- **`redact()` 的指数级回溯**：`_JSON_RE` 的两个分支在反斜杠上重叠，一串反斜杠
  （如解析报错时 `repr` 出来的畸形单元格）会让脱敏指数爆炸——实测 36 个反斜杠卡 19 秒，
  而且就发生在"报错要打印原因"的必经路径上。
- **飞书 webhook 明文进日志**：`requests` 的异常消息里带 URL/路径，发送失败时 hook id
  以前会原样打进控制台与 `--log-file`；现在按形态兜底遮掉（含没有 scheme 的裸路径）。
- **取值超范围不再静默入库**：decimal 的小数位/整数位、bigint 的 64 位范围在解析阶段校验
  （超了 MaxCompute 要么拒绝写入——那发生在"先删"之后、会形成缺数循环——要么按 scale 舍入）。
- **欧式小数不再被静默读错**：`1.234,56` 以前被读成 1.23456（缩小约 1000 倍）、
  `1,23` 被读成 123；现在只接受真正的千分位写法，其余报错。
- **大额合计不再被 28 位默认精度舍入**：decimal(29..38,x) 的列做合计核对时，`+=` 受
  decimal 默认 context 限制会被舍入，产生"合计行与数据行合计不一致"的假报错。
- **软链文件按目标大小核对**：READDIR 给的 `st_size` 是链接自身长度，以前下载后必然报
  "大小不一致"（软链支持实际不可用）。
- `parse.footer.sum` 不能包含第一列（合计行靠「首列为空」识别，那列取不到合计值），
  配置阶段拦下；`auth.type` 的值不再被当成密钥值（日志里正常的 "password" 一词不再被遮）。
- `retry_call` 的最终异常带上 `from`，保留原始异常链与类型线索。

### 文档

- README 退出码表补充 `--check` 不判缺文件；`allow_empty` 写明写空前的分区检查与 `--force`；
  `footer.sum` 写明只对入库行求和；迁移章节写明平铺回退只用于跳过判定。

## [1.0.0] - 2026-09-24

首个版本：从 `clink_settlement_sync` / `waffo_settlement_sync` 两个独立脚本抽象成
配置驱动的通用 SFTP → MaxCompute ODS 同步工具。

### 功能

- 作业配置驱动（`jobs/*.json`）：SFTP 连接与认证（密码/私钥）、远端文件规则
  （`flat` 平铺 / `date_dir` 日期子目录）、列定义（`string`/`bigint`/`double`/`decimal(p,s)`）、
  目标表、缺文件核对、飞书告警。
- 解析：按列头名映射（忽略大小写/空白），必需列缺失报错、可选列缺失告警按空入库；
  行-列数严格校验；空值默认存 NULL（`parse.empty_as: empty` 可让空串原样存 `''`，
  兼容旧 clink 表口径）；金额解析失败报错；可选的"合计行（首列为空）不写库 +
  金额 = 数据行之和"校验；自动探测分隔符（Tab/逗号/分号）；显式编码（含 GBK）。
- 写入：`pt` = 文件日期（一个日期一个分区，一天多文件合并）；自动建表/结构校验
  （列名、顺序、类型、分区）；先删再填；Tunnel 分批写入；写后 `count(*)` 复核；
  写库前先查单格大小（超 7MB 提前失败）。
- 可靠性：SFTP 操作重试（认证失败快速失败）、下载 `.part` + 大小核对、缺文件核对
  （内部断档 + 最新缺失，可配时区与"每日生成时刻"宽限）、空远端告警、运行锁、
  Ctrl+C 不写库、密钥脱敏（形态 + 配置值）、SQL 超时保护、退出码 0/1/2/130。
- 复审加固：金额拒绝 NaN/Infinity（decimal → 非有限数直接报错）；私钥文件缺失/需口令
  快速失败不空等重试；台账记录带项目名（换目标项目不会拿旧项目记录跳过）；`footer.sum`
  仅允许 decimal 列（配置阶段就拦住）；远端软链文件兼容；`--check --bizdate` 支持单日核对；
  `--start-date > --end-date`、负 `--sql-timeout` 等入口参数加固。
- 台账（下载目录 `.uploaded.json`）：表/pt/大小 命中即跳过；`--force` 强制重写；
  与两个旧脚本的台账字段兼容，`source.download_dir` 指到旧目录即可复用已下载文件。
- `--check` 体检（配置 + 真实列目录 + 目标表结构）、`--dry-run` 试跑、`--init` 交互式向导
  （可直接连 SFTP 拉样本文件生成列定义）、`--start-date/--end-date` 补数。
- CLI 入口 `sftp2ods`（console script）与 `python -m sftp2ods` 两种用法。

### 示例

- `jobs/clink_settlement.example.json`：Clink 结算文件（等价旧脚本）。
- `jobs/waffo_settlement.example.json`：Waffo 结算明细（等价旧脚本）。
- `jobs/_template_full.example.json` / `jobs/_template_simple.example.json`。

### 工程

- 239 个离线单元测试（假 SFTP + 假 MaxCompute，不访问网络/数仓）。
- GitHub Actions：ubuntu/windows/macos × Python 3.9 ~ 3.14，跑 `ruff check` + `ruff format --check` + 单元测试。
- 代码统一为 `ruff format` 口径（纯排版改动，无行为变化）；开发依赖 `ruff==0.16.8` 锁版本，
  保证本地与 CI 的格式判定完全一致。
- README 补充 Windows 安装与激活虚拟环境的命令，安装说明区分源码安装与 Git 安装；
  Python 版本徽章对齐 CI（3.9 ~ 3.14）。
- `--force` / `--dry-run` 的帮助文案措辞修正（行为不变）。
