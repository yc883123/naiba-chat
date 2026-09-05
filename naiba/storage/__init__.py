"""storage 层：数据持久化与宿主媒体缓存（层级 2 起点）。

ChatStorage（SQLite）主体仍位于根目录 storage.py，随阶段 3 迁入；
本包目前承载可独立参数化的媒体缓存工具（naiba/storage/media.py）。
"""
