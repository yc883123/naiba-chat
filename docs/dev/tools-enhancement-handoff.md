# 宸ュ叿绯荤粺澧炲己 路 闃舵浜ゆ帴鏂囨。

> **鎬ц川**锛氶樁娈垫€т氦鎺ユ枃妗ｏ紙褰撳墠浼氳瘽绐楀彛灏嗙粨鏉燂紝渚涙柊浼氳瘽绐楀彛鎺ユ墜锛夈€?> 瀹屾垚鍚庨殢浣垮懡鍒犻櫎锛堜笌 docs/dev 鏃㈡湁绾﹀畾涓€鑷达級锛涙湡闂翠互 `椤圭洰缁存姢璇存槑锛堜慨鏀逛唬鐮佸墠蹇呰锛?md` 涓烘潈濞佺幇鐘惰鏄庛€?
---

## 涓€銆佸綋鍓嶇姸鎬侊紙鎺ユ墜鍗虫锛?
- **鍒嗘敮**锛歚master`锛孒EAD = **`e5e2567`**锛堝伐鍏锋弿杩扮槮韬細鍧囪　妗ｏ級锛?- **楠岃瘉鍩虹嚎**锛歚python -m unittest discover -s tests` = **159 鐢ㄤ緥 OK**锛沗tests.golden_replay` = 3 鍩虹嚎 OK锛沗.tmptest/scan_undef_all.py` = 0 鍊欓€夛紱宸ュ叿娉ㄥ唽 def 鎬绘暟 = **32**锛堟ā鍨?Web 鍙 `schemas()` = **27**锛夛紱
- **鍐荤粨鐗?*锛歚dist\naiba-chat.exe`锛堝惈 701debd 鍏ㄩ儴淇锛涙湰闃舵浜х墿鏈噸鏂扮紪璇戯紝鍙戝竷鏃舵寜 搂浜?閲嶇紪锛夛紱
- **鍒嗘敮璇存槑**锛歚test1` 涓鸿瘖鏂疄楠屽垎鏀紙浠嶄繚鐣欐枃浠舵棩蹇?+ `NAIBA_NO_REASONING_PASSBACK` 寮€鍏筹紝鏈苟鍏?master锛夛紱master 宸ヤ綔鏍戦櫎 `build_info.json` 澶栧共鍑€锛堢鍚堣鍒欙級銆?
## 浜屻€佹湰闃舵鐩爣涓庡凡瀹屾垚锛坢aster 鎻愪氦閾撅級

| 鎻愪氦 | 鍐呭 |
|---|---|
| 54061b4 | 鍒犻櫎 `call_mcp` 缃戝叧 + 閫€褰瑰悕寮曞锛坄RETIRED_TOOL_GUIDE/MAP`锛涢厤缃縼绉绘竻娲楁棫鍚嶏級 |
| 04a838c | 瑙嗚鍗曞叆鍙ｉ噸鏋勶細8鈫?锛坄vision_analyze` 鍚屽悕浼氳瘽鍖栧垎娴?+ `vision_image_ops` PIL 涓夊悎涓€锛夛紱鍒犻櫎妯″瀷鑳藉姏鏄犲皠鏃忎笌 3 澶?`startswith("vision_")` 杩囨护锛汻unContext 鏂板绾﹂敭 `model_has_vision`/`tool_defs`锛?0 閿級 |
| 9e75ebb | 鐮村潖鎬у彉鏇磋惤鐐癸紙release_notes/update/README/缁存姢璇存槑锛?|
| 4562198 | `tool_rejection` 绮剧‘鍖归厤锛堣８璇?`tools`/`unsupported` 闃茶瑙﹀彂锛?|
| 586203f | Revert b0f8cdf锛堥敊璇舰鎬佸洖閫€鈥斺€斿巻鍙诧紝鍕垮啀鍥炶俯锛?|
| 6ad8ee9 | DeepSeek 鎬濊€冨洖浼狅紙棣栫増鑽夋锛? 鏃犲伐鍏峰厹搴曪紙400 鏃跺幓 tools 閲嶈瘯涓€娆★級 |
| 25193d7 | 鍥炰紶椤哄簭淇锛坒unction_call 閰嶅锛? 鍥剧墖璁板繂浣滅敤鍩熸敹鏁涳紙`_path_cache` 璺ㄤ細璇濇畫鐣欙紝鐢ㄥ悗鍗崇剼锛?|
| 701debd | **鏈€缁堝悎娉曞舰鎬?*锛歚{"type":"reasoning","content":[{"type":"reasoning_text","text":"鈥?}]}` 鍧楁暟缁勶紱椤哄簭 `reasoning 鈫?assistant 娑堟伅 鈫?function_call 鈫?function_call_output`锛涘疄娴嬮€氳繃 |
| `<21162ab>` | **瑙嗚璋冪敤缁熶竴涓烘ā鍨嬮┍鍔?*锛氱Щ闄ゃ€岃嚜鍔ㄨ矾鐢便€嶉€夐」涓庡悗鍙拌瘑鍥炬敞鍏ワ紙閰嶇疆榛樿/杩佺Щ娓呮礂/`prepare_history` 绾崰浣嶏級锛涘垹闄よ矾鐢辩紦瀛樹笌鍥剧墖璁板繂姝讳唬鐮侊紱`vision_start/vision_done/vision_error` 浜嬩欢濂戠害涓庡墠绔鐞嗕竴骞剁Щ闄わ紙瑙嗚宸ュ叿涓庡叾瀹冨伐鍏峰悓鏋勫憟鐜帮級锛涘浘鐗囧鐞嗙瓥鐣ユ彁绀烘敼鍐欙紱鍓嶇璁剧疆椤靛垹闄よ嚜鍔ㄨ矾鐢辫 |
| `2cc14ea` | **娓呯悊 run/manager 褰卞瓙鏃х被**锛氬垹闄よ `chat.py::ConversationRunMixin` 閬斀鐨勬棫鐗?`ConversationRunManager`锛堟棫 `submit_chat/_run_chat` 鍏ㄦ枃鍓湰锛夊強 15 涓殢涔嬪け鏁堢殑瀵煎叆 |
| `<76b84a4>` | **Harness 鍒悕闅愯棌**锛歚read/write/edit/glob/grep` 浠庢ā鍨?Web 鍙闆嗭紙`schemas()`锛夐殣钘忥紝鍙繚鐣欐煡璇㈠眰褰掍竴锛坮esolve/get/execute 鍏煎锛夛紱`HARNESS_TOOLS` 鍙敞鍏ヨ鑼冨悕锛涘畧闂細鍒悕涓嶅叆 schemas/allowed_tools銆乺esolve/get 鍏煎銆佺粍瑁呮€佹墽琛岀粦瀹氳鐩栧埆鍚?def |
| `e5e2567` | **宸ュ叿鎻忚堪鐦﹁韩锛堝潎琛℃。锛?*锛氭弿杩?鈮?00 瀛?鈮? 鍙ャ€佸垹鍐呴儴鏈锛圚arness/瀹夸富锛変笌璺ㄥ伐鍏风紪鎺掗暱鍙ュ紡锛涜法宸ュ叿瑙勫垯杩佺郴缁熸彁绀哄父椹诲尯锛圕omfyUI 涓ゆ鍚堜竴銆丣ob ID 绾緥鍙ユ硶浼樺寲锛夛紱http_request method 鍙傛暟鏀?enum锛涘畧闂?`tests/test_tool_description_slim.py`锛? 鏉★級锛涘鐓ф枃妗?`docs/dev/tools-description-slim.md` |

**鍏朵綑鎴愭灉**锛堟洿鏃╅樁娈碉紝宸插湪 master锛夛細宸ュ叿鍗曚竴瀹氫箟鏋舵瀯锛坄ToolSpec`/`providers/*`/鍗曟彃妲藉垎鍙?寮曟搸鐦﹁韩锛夈€佺‘璁ら摼淇锛坧olicy 宸ヤ綔鍖?NEED_CONFIRM 鍐掑彿/鍓嶇濮旀墭锛夈€乬olden 鍩虹嚎浣撶郴銆?
## 涓夈€佸叧閿璁＄粓鎬侊紙宸插疄娴嬬‘璇侊紝鎺ユ墜鍕挎敼锛?
1. **DeepSeek reasoning 鍥炰紶**锛坄naiba/llm/protocols.py::_responses_input`锛夛細
   - 褰㈡€佸繀椤绘槸 `content` 鍧楁暟缁勶紙鐞嗙敱瑙佺淮鎶よ鏄?搂涔?12 鏉★細`reasoning_text` 瀛楁鏈嶅姟绔笉璁ゃ€佹槑鏂?content 琚?serde 鎷掞級锛?   - 椤哄簭锛歳easoning item 鈫?assistant 娑堟伅锛堟湁鍊兼椂锛夆啋 function_call 鈫?function_call_output锛堥厤瀵圭浉閭伙紝涓棿鎻掍换浣?item 浼?"No tool output found"锛夛紱
   - 鏃?reasoning 鏃朵笉浜?item銆?2. **瑙嗚璋冪敤缁熶竴鐢辨ā鍨嬮┍鍔紙宸插疄鐜扮殑缁堟€侊紝鎺ユ墜鍕挎敼锛?*锛?   - 銆岃嚜鍔ㄨ矾鐢便€嶉€夐」宸茬Щ闄わ細绾枃鏈ā鍨嬩笂浼犲浘鐗囨椂锛宍prepare_history` 鍙仛瀹夊叏鍗犱綅鏀瑰啓
     锛堣矾寰勫紩鐢?+ 璋冪敤鎻愮ず锛夛紝**涓嶅啀鍚庡彴璋冪敤瑙嗚鍚庣**锛涗綍鏃剁湅鍥俱€侀棶浠€涔堢敱妯″瀷鎸夐渶
     璋冪敤 `vision_analyze` 宸ュ叿鍐冲畾锛岀粨鏋滀笌鍏跺畠宸ュ叿涓€鏍蜂互宸ュ叿鍧楀憟鐜般€?   - 澶氭ā鎬佹ā鍨嬩粛鐩存帴鏀跺埌鍘熷浘锛堜笉鍋氬崰浣嶆敼鍐欙級锛屾棤闇€璋冪敤瑙嗚宸ュ叿銆?   - 璺敱缂撳瓨 / 鍥剧墖璁板繂锛坄_route_cache*` / `_path_cache*` / `_apply_image_memory`锛夊凡闅?     鑷姩璺敱涓€骞跺垹闄わ紝鍕垮娲伙紱`vision_start/vision_done/vision_error` 浜嬩欢宸蹭粠濂戠害涓庡墠绔Щ闄ゃ€?   - 閰嶇疆娈嬬暀閿?`vision.auto_route` 鐢?ConfigStore 鍚姩鏃舵竻娲楋紙`config.py`锛夈€?3. **瑙嗚妯″瀷宸紓鏄湇鍔＄鐗规€?*锛欴eepSeek 鍙 `deepseek-v4-flash`/`v4-pro` 鏍￠獙 thinking 鍥炰紶锛宍vision-exp` 涓嶆牎楠岋紙璇婃柇璇佸疄涓ゆā鍨嬪鎴风璇锋眰瀹屽叏鍚屾瀯锛夈€?*鏃犻渶涓?vision-exp 鍋氶€傞厤**銆?4. **璇婃柇绾緥**锛歸indowed exe 鏃犳帶鍒跺彴銆佸簲鐢ㄦ棤 logging handler 鈫?**璇婃柇鍐欐枃浠?*锛坱est1 鍒嗘敮鐨?`_write_model_debug` 妯″紡鍙鐢紱`AllocConsole` 鏂规瀹炴祴鏃犳晥锛夈€?
## 鍥涖€佹湭瀹屾垚 / 涓嬩竴姝ワ紙渚涙帴鎵嬮€夋嫨锛?
1. **鍙戝竷娓呭崟鎵ц**锛堝彂甯冩椂锛夛細`naiba-chat-update.json` 鐨?`commit`/`sha256` 鐢?workflow 瑕嗙洊锛沗README.md` 鑳藉姏鍒楄〃鎸夐渶寰皟锛涘喕缁撶増缂栬瘧锛堣 搂浜旓級銆?2. 娴忚鍣ㄥ啋鐑燂紙Playwright锛夐渶鐢ㄦ埛鏈満 `npm install playwright` + Edge 鏉冮檺锛屾湰 DSH 娌欑琚嫤锛堝凡鐭ワ級銆?3. `docs/manual/`锛堟墜鍔ㄦ枃妗ｄ笌鎴浘锛夊綋鍓嶆寜鐢ㄦ埛鎸囩ず涓嶅啀缁存姢锛堟棫鍥惧惈宸茬Щ闄ょ殑鑷姩璺敱琛岋紝淇濈暀鐜扮姸锛夈€?4. **寰呯敤鎴峰疄娴?*锛氭弿杩扮槮韬悗鐨勭悊瑙ｈ川閲忊€斺€旀枃鏈ā鍨?+ 宸ュ叿杞笁鍦烘櫙锛堣瘑鍥俱€佸悗鍙?Job銆丆omfyUI銆屾敼鏂囦欢鍐嶅紩鐢ㄣ€嶏級銆?
## 浜斻€侀獙璇佷笌澶嶆祴鍛戒护

```powershell
# 鐜锛堟湰 DSH 娌欑蹇呴』锛涙甯告湰鏈轰粎闇€ venv锛?$env:TEMP = "D:\naiba-chat\.tmptest"; $env:TMP = "D:\naiba-chat\.tmptest"; $env:PYTHONPATH = "D:\naiba-chat\.tmptest"

# 鍏ㄩ噺娴嬭瘯
& "D:\naiba-chat\.venv\Scripts\python.exe" -m unittest discover -s tests
& "D:\naiba-chat\.venv\Scripts\python.exe" -m unittest tests.golden_replay
& "D:\naiba-chat\.venv\Scripts\python.exe" .tmptest\scan_undef_all.py

# 浜旂偣鍐掔儫锛堝厛纭 8765 鏃犳棫瀹炰緥鈥斺€旀湰浼氳瘽鏇捐俯杩?鎵撳埌鐢ㄦ埛鏃?server 瀵艰嚧鍋囬槼鎬?鐨勫潙锛?# 缂栬瘧锛堝厛 kill 鍏ㄩ儴 naiba-chat 杩涚▼锛?$env:NAIBA_BUILD_VERSION = "2.0.0-beta"; & "D:\naiba-chat\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean naiba-chat.spec
```

**鐢ㄦ埛瀹炴祴瑕佺偣**锛氭枃鏈ā鍨嬶紙deepseek-v4-flash锛屾€濊€冩ā寮忥級+ 宸ュ叿璋冪敤锛堝惈 vision_analyze 璇嗗浘锛夊叏娴佺▼搴旀棤 400锛涙枃鏈ā鍨嬩紶鍥惧悗搴旂湅鍒般€岃矾寰勫崰浣?+ 妯″瀷涓诲姩璋冪敤 vision_analyze锛堝伐鍏峰潡鍛堢幇锛夈€嶏紝涓嶅啀鍑虹幇鑷姩璇嗗浘 status銆?
## 鍏€佹湰娆¤Е纰版枃浠讹紙master 澧為噺锛?
- 鍚庣锛堣閲嶆瀯/瑙嗚/鍗忚/閰嶇疆/宸ュ叿/浼氳瘽/鎶€鑳斤級锛歚naiba/{vision/runtime,run/chat,run/stream,run/session,run/manager,core/contracts,skills/agent,tools/registry,config,app}.py`
- 鍓嶇锛歚public/{index.html,styles.css,js/09-settings.js,js/11-run-stream.js,js/12-chat-input.js}`
- 娴嬭瘯锛歚tests/{test_config_migration,test_vision_prepare_history,test_tool_registry_shape,test_tool_description_slim}.py`
- 鍙戝竷/鏂囨。锛歚release_notes.json`銆乣README.md`銆乣椤圭洰缁存姢璇存槑锛堜慨鏀逛唬鐮佸墠蹇呰锛?md`銆乣docs/dev/tools-enhancement-handoff.md`锛堟湰鏂囨。锛夈€乣docs/dev/tools-description-slim.md`锛堢槮韬鐓э紝璇勫鍚庡垹闄わ級銆乣鐗堟湰鏇存柊璇存槑.md`锛堟湰鍦帮紝gitignored锛夈€?*`docs/manual/` 鎸夌敤鎴锋寚绀轰笉鍐嶇淮鎶ゃ€?*
- 楠岃瘉鑴氭湰锛坓itignored锛夛細`.tmptest/esm_graph_check.py` 宸蹭慨澶嶏紙鍓ョ娉ㄩ噴/瀛楃涓?妯℃澘/姝ｅ垯鍚庣殑璇嶈竟鐣屾壂鎻忥紝娑堥櫎姝ｅ垯瀛楅潰閲忚鎶ワ紱鍚堟垚鐢ㄤ緥楠岃瘉鐪熺己 import 浠嶈兘妫€鍑猴級

## 涓冦€佷氦鎺ョ粨璇?
宸ュ叿绯荤粺缁熶竴鏋舵瀯銆佸寮恒€佽瑙夐┍鍔ㄥ寲涓庡埆鍚嶉殣钘忓凡鍏ㄩ儴钀藉湴骞堕€氳繃楠岃瘉锛況un/manager 鎷嗗垎閬楃暀锛堝奖瀛愮被锛夊凡娓呯悊銆傚綋鍓嶆病鏈変换浣曞凡鐭ユ湭淇?bug銆傚墿浣欏伐浣滃潎涓哄閲忎紭鍖栦笌鍙戝竷鍔ㄤ綔銆傛帴鎵嬬涓€姝ワ細`git status` 纭骞插噣 + 璺?搂浜?鍏ㄩ噺娴嬭瘯锛?0 绉掞級纭鍩虹嚎銆?