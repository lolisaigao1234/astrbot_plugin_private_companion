"""隐私与现代触及类原生能力的默认授权。

这些能力直接读取用户所处的现实环境（桌面屏幕、摄像头、手机定位）。它们的默认
受众是核心管理员：核心管理员不需要逐窗口同意，也不受群聊/私聊范围限制；其他
人仍需按原来的逐窗口授权流程。

范围本身不再是门槛——同一份授权在哪个会话里都适用；是否允许只看主体。
"""

from companion_v2.identity_policy.models import AuthorizationTier


PRIVACY_ACTIONS = frozenset((
    "screen.capture",
    "reality.snapshot.read",
    "reality.camera.observe",
))

# 设备控制不是隐私读取，但核心管理员在群聊里也应保留与私聊相同的现实设备
# 权限；其他人仍会被 Reality 的 admin_only 与逐窗口授权挡住。
CORE_GROUP_ACTIONS = frozenset((
    "reality.device.list",
    "reality.device.status",
    "reality.device.inspect",
    "reality.device.control",
    "reality.device.action",
    "reality.operation.lookup",
))


def subject_tier(runtime, subject) -> AuthorizationTier:
    """Read the current access tier without mutating identity state.

    Pass the authenticated actor wherever one exists: a subject alone cannot
    tell a joined link principal from the conversation's owner.
    """
    from companion_v2.identity_policy.object_access import access_tier
    return access_tier(runtime, subject)


def is_core(runtime, subject) -> bool:
    return subject_tier(runtime, subject) is AuthorizationTier.CORE
