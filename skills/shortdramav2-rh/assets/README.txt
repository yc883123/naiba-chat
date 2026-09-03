参考图 / 参考音频存放目录。

config.json 的 pictures / audios（以及按段覆盖 segment_pictures / segment_audios）
里写的文件名在此目录解析（也支持写绝对路径）。

支持格式：
- 参考图：png / jpg / jpeg / webp
- 参考音：mp3 / wav / flac

注意：上传到 RunningHub 的链接仅 1 天有效，脚本会对同一文件去重上传并在
「NN单元输出/rh_uploads.json」缓存，超过 24 小时会自动重新上传。
