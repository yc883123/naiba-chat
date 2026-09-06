"""tools.providers：按域提供工具定义（ToolSpec 列表）。

每域一个模块（core/jobs/capability/vision/search/comfyui），
``tools() -> list[ToolSpec]`` 返回单一定义规格；实现函数在本模块内，
依赖通过构造参数注入（不摸 APP 全局）。
"""
