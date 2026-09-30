# B站直播回放（web-cut 剪辑系统）接口调研笔记

调研对象：某已获授权的目标主播（uid 形如 `349xxxxxxxxxxxxxxx`）
来源：web-cut 前端 JS 逆向 + bilibili-API-collect 镜像文档 + 无登录/有登录实测

## 结论

- **日期解析**：列表接口直接返回 `start_time` / `end_time`（Unix 秒），日期无需额外抓取。
- **完整录播下载**：可行，走 HLS 路径（已端到端实测通过，见下）。
- **两个硬边界**：
  1. **回放只保留约 14 天**（`GetOtherSliceList` 的 `time_range` 最大 3=近14天，过期即清）→ LuboDown 必须**定期自动抓取**，不能事后补旧账。
  2. **所有接口要求登录**（SESSDATA）；访问**别人**的回放还需该主播授予"回放剪辑权限"（无权限返回 `code=301`）。

## 实测结果（授权账号访问目标主播场次）

| 验证点 | 结果 |
| --- | --- |
| `AnchorGetReplayList?anchor_id=他人uid` | code=0 但列表为空（**该接口只返回登录账号自己的场次**，anchor_id 参数无效） |
| `GetOtherSliceList?live_uid={目标主播uid}` | ✅ code=0，近14天场次列表，日期/标题/live_key 齐全 |
| `GetSliceStream`（自己）访问他人场次 | code=202 无有效合成场次（符合预期，他人场次要用下面的） |
| `GetUserSliceStream?...&live_uid=` | ✅ code=0，返回 1 段 HLS |
| m3u8 直取（无 Cookie） | ✅ HTTP 200，2.57MB 播放列表，`application/vnd.apple.mpegurl` |
| 播放列表内容 | 8104 分段，`#EXT-X-ENDLIST`（完整 VOD），总时长正好 4.50h，无 init 段、每段自带 ftyp（独立 MP4 分片） |
| 分段直取（无 Cookie） | ✅ HTTP 200，`ftypisom` MP4 数据，CDN 为 d1--cn-*.bilivideo.com |
| ffmpeg 合成验证 | ✅ `ffmpeg -i <m3u8> -t 60 -c copy` → 60.02s、1920×1080 h264 + aac（77.7MB ≈ 10.3Mbps） |
| 整场估算体量 | 4.5h ≈ 20GB（1080p 高码率），CDN 下载速度快 |
| `AnchorVideoDownload`（单MP4合成） | ❌ code=210 无效场次——**只认自己直播间的场次**，无 `live_uid` 变体；授权剪辑者拿不到单MP4直链，下载统一走 HLS |

**LuboDown 下载路径定型**：`GetOtherSliceList`（列表+日期过滤）→ `GetUserSliceStream`（m3u8）→ ffmpeg/分段下载合成 MP4。
Cookie 仅调 api.live.bilibili.com 的两个接口时需要，m3u8 与分段均为公开 CDN 直链。

## 接口链路（host 均为 api.live.bilibili.com）

| 步骤 | 接口 | 方法/鉴权 | 说明 |
| --- | --- | --- | --- |
| 列表(自己) | `/xlive/app-blink/v1/anchorVideo/AnchorGetReplayList?anchor_id={uid}&page=&page_size=` | GET + Cookie | page_size≤30；返回 `replay_info[]`、`pagination.total` |
| 列表(他人) | `/xlive/web-room/v1/videoService/GetOtherSliceList?live_uid={uid}&time_range=&page=` | GET + Cookie | 需主播授权；time_range: 1=3天 2=7天 3=14天 |
| 切片流(自己) | `/xlive/app-blink/v1/anchorVideo/GetSliceStream?live_key=&start_time=&end_time=` | GET + Cookie | 返回 `data.list[]`：`{start_time,end_time,stream,type}`；`stream` 为 HLS 播放列表地址，分段带签名 query |
| 切片流(他人) | `/xlive/web-room/v1/videoService/GetUserSliceStream?...&live_uid={uid}` | GET + Cookie | 同上，需授权 |
| 整场合成 | `/xlive/app-blink/v1/anchorVideo/AnchorVideoDownload` | POST + Cookie + `csrf`(=bili_jct) | 参数 `live_key` 或 `record_id` 二选一 |
| 轮询状态 | `/xlive/app-blink/v1/anchorVideo/GetAnchorVideoUidRecord` | POST + csrf | `records=record_id`；status=30 完成，-30 失败 |

### 整场下载流程（官方"下载回放"按钮的流程）

1. POST `AnchorVideoDownload`（live_key/record_id + csrf）→ 触发 record2vod 合成；
2. 轮询 `GetAnchorVideoUidRecord` 直至 `status=30`；
3. 再次 POST `AnchorVideoDownload` → `data.download_url` 为 upos 签名直链
   （`https://upos-*.bilivideo.com/ugcever/*.mp4?...&attname=直播回放_YYYY-MM-DD_HH-MM-SS.mp4`），直接 GET 即可下载完整 MP4。

### 回放列表项字段（日期解析就绪）

```
replay_id, live_key, room_id, start_time, end_time,
live_info{title, cover, live_time, live_type, platform},
video_info{replay_status, duration, alert_code, alert_message},
alarm_info{code, message, is_ban_publish}
```

## 关键错误码

- `-101` 未登录（无 Cookie 实测确认）
- `-111` csrf 校验失败
- `301` 没有剪辑权限（未获主播授权）
- `202` 场次无效（live_key 与 start/end 不匹配）
- `-30` 回放合成失败；`30` 合成完成

## 验证工具

程序本体 `app/`（启动方式见 README）。安全层：仅 https、bilibili 域名白名单、解析 IP 须全部为公网、连接固定到已校验 IP（防 DNS rebinding）、重定向逐跳复检。
