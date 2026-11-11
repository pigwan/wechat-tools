"""本地能力注册表：把「功能需求清单」逐项映射到本机微信桥接的真实实现。

零外部依赖、零网络、零 Token。所有可用能力最终都调用
vendor/helper-main/wxbot_framework 的 ServiceFacade（即插件里的 ctx._service），
也就是直接驱动本机微信客户端，不经过任何第三方服务。

状态含义：
    native      底层原生支持，直接调用
    composite   用已有本地能力组合/近似实现（note 里写明差异）
    partial     只覆盖了需求的一部分（note 里写明缺口）
    unavailable 底层 C++ 桥未开放或需要逆向新版微信，本层不做假实现

catalog.json 只是功能名称对照表（由 tools/build_catalog.py 一次性生成，可手工编辑）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
CATALOG_PATH = _ROOT / "catalog.json"

# key|名称|模块|状态|handler|参数|对应需求名称(中文, 逗号分隔)|说明
_ROWS = """
message.postText|发送文本消息|消息|native|send_text|toWxid,content|发送文字消息|直接调用本地文本发送
message.postAtText|群内@文本|消息|native|send_at_text|room,content,ats|发送文字消息|ats 为 wxid 列表，逗号分隔；notify@all 可@全体
message.postImage|发送图片|消息|native|send_image|toWxid,path|发送图片消息|path 为本机图片绝对路径
message.postFile|发送文件|消息|native|send_file|toWxid,path|发送文件消息|path 为本机文件绝对路径
message.postVoice|发送语音|消息|native|send_voice|toWxid,path|发送语音消息|支持 silk / wav
message.postVideo|发送视频|消息|composite|send_file|toWxid,path|发送视频消息|本地桥以「文件」形式发送视频，非原生视频消息
message.postEmoji|发送表情|消息|native|send_emoticon|toWxid,path|发送emoji消息|本地 GIF 表情
message.postLink|发送链接|消息|composite|send_text|toWxid,content|发送链接消息|以纯文本发 URL，微信不会渲染成卡片
message.postLocation|发送定位|消息|composite|send_text|toWxid,content|发送定位|以文本描述位置，非原生定位消息
message.downloadImage|下载图片|消息|composite|local_media|kind,limit|下载图片|接收时图片已落盘到本地媒体目录，这里按类型检索
message.downloadFile|下载文件|消息|composite|local_media|kind,limit|下载文件|同上
message.downloadVoice|下载语音|消息|composite|local_media|kind,limit|下载语音|同上
message.downloadVideo|下载视频|消息|composite|local_media|kind,limit|下载视频|同上
message.downloadEmoji|下载表情|消息|composite|local_media|kind,limit|下载emoji|同上
message.forwardFile|转发文件|消息|partial|send_file|toWxid,path|转发文件|可重新发送本地文件，但无法按原消息 ID 转发
message.forwardImage|转发图片|消息|partial|send_image|toWxid,path|转发图片|同上
message.forwardVideo|转发视频|消息|partial|send_file|toWxid,path|转发视频|同上
message.forwardUrl|转发链接|消息|partial|send_text|toWxid,content|转发链接|以文本重发 URL
message.exportEmoticons|导出本地表情|消息|native|export_emoticons|convert|发送emoji消息|框架自带能力，GeWe 清单无对应项
contacts.fetchContactsList|获取通讯录|联系人|native|contacts|无参数|获取通讯录列表,获取通讯录列表缓存|只读本机已加载缓存，含资料与标签关联
contacts.getBriefInfo|获取简要信息|联系人|composite|contact_info|wxid|获取群/好友简要信息|从通讯录缓存中取
contacts.getDetailInfo|获取详细信息|联系人|composite|contact_info|wxid|获取群/好友详细信息|含标签关联
contacts.search|搜索好友|联系人|composite|search_contacts|keyword|搜索好友|在本地通讯录缓存里按昵称/微信号/备注筛选，不联网
contacts.checkRelation|检测好友关系|联系人|composite|contact_info|wxid|检测好友关系|判断是否在本地已缓存好友中
contacts.addContacts|通过好友申请|联系人|partial|accept_friend|requestId|添加联系人/同意添加好友|只能通过已收到的申请；无法主动搜索陌生人发起添加
contacts.friendRequests|好友申请列表|联系人|native|friend_requests|无参数|添加联系人/同意添加好友|框架自带能力
group.createChatroom|创建群聊|群|native|create_group|wxids|创建微信群|至少需要 2 位好友
group.inviteMember|邀请进群|群|native|invite_group_members|room,wxids|邀请/添加 进群|
group.getChatroomMemberList|群成员列表|群|native|group_members|room|获取群成员列表|
group.getChatroomMemberDetail|群成员详情|群|native|group_members|room|获取群成员详情|含群昵称与角色
group.getChatroomInfo|群信息|群|composite|group_info|room|获取群信息|由群名+成员数+管理员组合得出
group.addGroupMemberAsFriend|加群成员为好友|群|partial|accept_friend|requestId|添加群成员为好友|仅能处理已收到的好友申请
group.memberName|查群成员昵称|群|native|group_member_name|room,wxid|获取群成员详情|框架自带能力
group.memberRole|查群成员角色|群|native|group_member_role|room,wxid|获取群成员详情|框架自带能力
sns.sendTextSns|发文字朋友圈|朋友圈|native|publish_moments|content,privacy|发送文字朋友圈|
sns.sendImgSns|发图片朋友圈|朋友圈|native|publish_moments|content,images,privacy|发送图片朋友圈|images 为本机图片路径列表
sns.likeSns|点赞/取消点赞|朋友圈|native|interact_moments|snsId,action|点赞/取消点赞|依赖本地桥已开放点赞
sns.commentSns|评论/删除评论|朋友圈|native|interact_moments|snsId,action,content|评论/删除评论|依赖本地桥已开放评论
sns.snsList|自己的朋友圈列表|朋友圈|native|read_moments|limit|自己的朋友圈列表|读取本机已加载的朋友圈缓存
sns.contactsSnsList|联系人朋友圈列表|朋友圈|native|read_moments|limit|联系人的朋友圈列表|同上
sns.snsDetails|朋友圈详情|朋友圈|composite|read_moments|limit,snsId|某条朋友圈详情|从缓存结果中按 ID 过滤
sns.capabilities|朋友圈能力探测|朋友圈|native|moments_capabilities|无参数|发送文字朋友圈|返回当前微信版本实际开放的朋友圈能力
label.list|标签列表|标签|composite|labels|无参数|标签列表|从通讯录的好友标签关联聚合，只读
account.checkOnline|检查是否在线|账号|native|check_online|无参数|检查是否在线|检查本地微信桥接连接状态
account.getProfile|获取个人资料|账号|composite|profile|无参数|获取个人资料|返回当前绑定账号 wxid / 会话状态
"""

# 底层明确不支持的模块/原因（用于生成覆盖报告，不伪造实现）
_MODULE_REASONS = {
    "视频号模块": "本地 C++ 桥完全没有视频号相关入口，需要另写逆向桥接",
    "收藏夹模块": "本地桥未读取微信收藏夹数据结构",
    "个人模块": "改资料/头像/隐私设置需调用未开放的原生写入口；仅「获取个人资料」可用本地账号信息近似",
    "账号管理": "退出/代理设置属于框架启动参数，不在插件层暴露；仅「检查是否在线」可用",
    "登录模块": "本地是进程内 hook，无需扫码/设备登录/公网回调；框架自带微信登录面板",
}

_ACTION_REASONS = {
    "消息模块": "转发(按原消息ID)、主动撤回、名片/小程序/appmsg/合并消息等原生构造入口未开放",
    "联系人模块": "删除好友、改备注、仅聊天、手机通讯录、企微好友等写入口未开放",
    "群模块": "改名/踢人/退出/解散/公告/管理员/二维码/置顶/免打扰/扫码进群 等在 group_management.h 中未实测开放",
    "朋友圈模块": "删除、可见范围、隐私设置、转发、视频发布、上传CDN 等未开放",
    "标签模块": "标签为只读内存缓存，写入口未开放",
}


@dataclass(frozen=True)
class Action:
    key: str
    name: str
    module: str
    status: str
    handler: str
    params: tuple
    aliases: tuple
    note: str = ""

    @property
    def available(self) -> bool:
        return self.status in ("native", "composite", "partial")


def _parse_rows() -> dict:
    actions = {}
    for raw in _ROWS.strip().splitlines():
        parts = raw.split("|")
        if len(parts) < 7:
            continue
        key, name, module, status, handler, params, aliases = parts[:7]
        note = parts[7] if len(parts) > 7 else ""
        params = tuple(p.strip() for p in params.split(",") if p.strip() and p.strip() != "无参数")
        aliases = tuple(a.strip() for a in aliases.split(",") if a.strip())
        actions[key] = Action(key, name, module, status, handler, params, aliases, note)
    return actions


ACTIONS: dict[str, Action] = _parse_rows()


def load_catalog() -> list[dict]:
    if not CATALOG_PATH.is_file():
        return []
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return data.get("items", [])
    except (OSError, ValueError):
        return []


def match_action(need_name: str) -> Action | None:
    """按需求清单里的中文功能名找本地实现。"""
    for action in ACTIONS.values():
        if need_name in action.aliases:
            return action
    return None


def coverage() -> dict:
    """生成 134 项需求的本地覆盖报告。"""
    items = load_catalog()
    covered, uncovered = [], []
    by_module: dict[str, dict] = {}
    for item in items:
        mod = item.get("module", "其他")
        slot = by_module.setdefault(mod, {"total": 0, "covered": 0})
        slot["total"] += 1
        action = match_action(item.get("name", ""))
        if action:
            covered.append({"name": item["name"], "module": mod,
                            "action": action.key, "status": action.status})
            slot["covered"] += 1
        else:
            reason = (_ACTION_REASONS.get(mod)
                      or _MODULE_REASONS.get(mod)
                      or "本地桥未开放该写入口")
            uncovered.append({"name": item["name"], "module": mod, "reason": reason})
    return {
        "total": len(items),
        "covered": len(covered),
        "uncovered": len(uncovered),
        "actions": len(ACTIONS),
        "by_module": dict(sorted(by_module.items(), key=lambda kv: -kv[1]["total"])),
        "covered_items": covered,
        "uncovered_items": uncovered,
    }


def list_actions(module=None, keyword=None) -> list[Action]:
    out = []
    for action in ACTIONS.values():
        if module and module not in (action.module, action.key.split(".", 1)[0]):
            continue
        if keyword and keyword.lower() not in (action.key + action.name).lower():
            continue
        out.append(action)
    return out
