# DSH 沙箱专用临时补丁（不属于项目，勿提交）：
# 1) 沙箱按 POSIX 模式模拟 mkdir 权限：0o700（owner-only）会导致创建者自身被 ACL 拒绝
#    （WinError 5），而 tempfile.mkdtemp/TemporaryDirectory 硬编码 0o700。这里归一为 0o777。
# 2) 沙箱限制 os.chmod，tempfile 清理时的权限重置会失败；Windows 上 chmod 仅影响只读位，
#    禁用该重置不影响测试语义。
import os as _os
import tempfile

_orig_mkdir = _os.mkdir


def _mkdir_sandbox_workaround(path, mode=0o777, *args, **kwargs):
    if mode == 0o700:
        mode = 0o777
    return _orig_mkdir(path, mode, *args, **kwargs)


_os.mkdir = _mkdir_sandbox_workaround


def _noop_resetperms(path):
    return None


tempfile._resetperms = _noop_resetperms
