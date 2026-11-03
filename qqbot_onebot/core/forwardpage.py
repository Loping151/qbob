"""合并转发 -> 一个可点开的网页, 群里只发链接.

媒体一律热链 QQ CDN, 不托管媒体字节: 入站媒体原样用直链; 插件给的 base64/本地文件
建页时分片上传, merge 回的 file_uuid 即 fileid(见 cdnkeys); 外站链接原样热链.

直链需 rkey: 渲染时换上最近捡到的 rkey; 没有则退回链接自带 rkey(建页 1 小时内)或
COS 签名 raw_url(1 小时); 都没有才显示「已过期」.

spec 参数: 0 原图 / 198 小缩略图 / 720 短边 ≤720 中图. 格子放中图垫小图, 原图只在灯箱加载.
spec 变体会把动图压成单帧, 故 mface 和 .gif 用原图.

markdown 探测与渲染复用出站那套(looks_like_markdown + md_to_html), 保持一致.
页面无外部资源, 脚本用 CSP sha256 放行, 注入的 <script> 跑不了.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import time

from . import cdnkeys
from .textimg import looks_like_markdown, md_to_html

logger = logging.getLogger("qqbot.forwardpage")

# 链接自带 rkey 的有效期: 无新 rkey 时, 建页此时长内仍用原链接
MEDIA_TTL_SECONDS = 3600
# 嵌套转发最大展开层数(sender 展开 forward id 共用; 也防环)
NEST_MAX_DEPTH = 3


def resolve_base(own: str, config=None) -> tuple[str, str]:
    """转发网页根地址与来源: 插件自填 > forward_base_url > public_base_url."""
    own = str(own or "").strip()
    if own:
        return own.rstrip("/"), "plugin"
    for key in ("forward_base_url", "public_base_url"):
        value = str(getattr(config, key, "") or "").strip()
        if value:
            return value.rstrip("/"), key
    return "", ""


def page_base(config=None) -> tuple[str, str]:
    """本服务转发网页(和卡片封面)对外的根地址与来源, 插件「合并转发转网页」的设置优先.

    插件注册表在函数里才导入: 外部项目 import 本模块只为渲染, 不碰插件.
    """
    from ..plugin import registry  # noqa: PLC0415
    registry.ensure_loaded()
    plugin = registry.plugins.get("forward_page")
    return resolve_base(plugin.config.get("base_url") if plugin else "", config)

_MEDIA_TYPES = ("image", "mface", "video", "record", "file")
_KIND_LABEL = {"image": "图片", "mface": "表情", "video": "视频",
               "record": "语音", "file": "文件"}

# 只认 http(s): 同 origin 挂着 /admin, 不能放 data:/javascript: 造成 XSS
_OK_SCHEMES = ("https://", "http://")

# 裸链接自动成链; 在转义后的文本上匹配, 不会匹配到标签
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TAIL = "。，、；：！？…）】》」』.,;:!?)"

# QQ CDN 直链尺寸变体, 只改已有的 spec 参数
_QQ_CDN = "https://multimedia.nt.qq.com.cn/"
_SPEC_RE = re.compile(r"([?&])spec=-?\d+")
_SPEC_THUMB, _SPEC_MEDIUM = 198, 720

# 头像色相: 按名字取, 同一人整页同色
_HUES = (212, 160, 24, 340, 262, 188, 44, 300)

_STYLE = """
:root{color-scheme:light dark;--bg:#f2f2f7;--sheet:#fff;--ink:#1c1c1e;--mute:#8e8e93;
--line:rgba(60,60,67,.12);--tile:#e9e9ee;--hatch:#dcdce2;--link:#0a7aff;--bar:rgba(242,242,247,.84);
--al:72%;--aa:.2;--abg:linear-gradient(160deg,#fff,#eef0f6);--pane:hsl(var(--h1,212) 52% 98%);
--bar2:rgba(255,255,255,.56);--gsh:0 1px 2px rgba(15,23,42,.05),0 8px 24px rgba(15,23,42,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#000;--sheet:#1c1c1e;--ink:#f2f2f7;
--mute:#8e8e93;--line:rgba(84,84,88,.48);--tile:#2c2c2e;--hatch:#3a3a3c;--link:#409cff;
--bar:rgba(0,0,0,.72);--al:42%;--aa:.24;--abg:linear-gradient(160deg,#0d0e12,#16171c);
--pane:hsl(var(--h1,212) 15% 12%);--bar2:rgba(16,16,20,.5);--gsh:0 8px 24px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
/* 极光画在 body 自己的背景上, 不另起层. 原来那层 position:fixed 的 ::before 会把
整页拖进合成路径, 而 .sheet 是整页高 —— 实测安卓 Edge 上面板底色画不满(图片露到
背景外面), QQ 内置浏览器上整块糊成灰的还带一条噪点. body 的背景由根画布直接铺,
不产生任何层. 代价是极光不跟着视口走, 只铺首屏那一段, 往下滚剩底色渐变 —— 兜底
层, 认了. 极光单独写在 background-image 里: 万一老浏览器不认新版 hsl() 语法,
掉的只是这一条, 底色还在. */
body{margin:0;background:var(--bg);background-image:
radial-gradient(70vw 42vh at 8% 3vh,hsl(var(--h1,212) 60% var(--al) / var(--aa)),transparent 70%),
radial-gradient(70vw 42vh at 94% 24vh,hsl(var(--h2,44) 60% var(--al) / var(--aa)),transparent 70%),
radial-gradient(70vw 42vh at 14% 72vh,hsl(var(--h3,160) 60% var(--al) / var(--aa)),transparent 70%),
var(--abg);color:var(--ink);font:15.5px/1.6 -apple-system,
"PingFang SC","HarmonyOS Sans SC","MiSans","Noto Sans CJK SC","Microsoft YaHei",
system-ui,sans-serif;-webkit-font-smoothing:antialiased}
/* 不吃 env(safe-area-inset-*): 这页任何时候都在浏览器 UI 底下, 用不着躲刘海.
安卓 QQ 内置浏览器上面还顶着自己的标题栏, 却照报设备刘海的 ~57px, 顶栏白白空出
一条带 —— 去掉 viewport-fit=cover 都止不住, 只能不用它. */
.top{position:sticky;top:0;z-index:2;background:var(--bar);-webkit-backdrop-filter:
saturate(180%) blur(14px);backdrop-filter:saturate(180%) blur(14px);
border-bottom:1px solid var(--line)}
.bar{max-width:680px;margin:0 auto;padding:12px 16px;display:flex;align-items:baseline;
gap:10px}
h1{font-size:17px;font-weight:600;margin:0;letter-spacing:-.01em}
.cnt{color:var(--mute);font-size:13px;font-variant-numeric:tabular-nums;margin-left:auto}
.sheet{max-width:680px;margin:12px auto 0;background:var(--sheet);border-radius:14px;
padding:4px 0;overflow:hidden}
@media(max-width:700px){.sheet{margin-top:0;border-radius:0}}
.day{display:table;margin:12px auto 2px;padding:2px 12px;border-radius:999px;background:var(--tile);
color:var(--mute);font-size:12px;font-variant-numeric:tabular-nums}
.g{display:flex;gap:12px;padding:12px 16px;border-top:1px solid var(--line)}
.day+.g,.sheet>.g:first-child{border-top:0}
.av{flex:none;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;
justify-content:center;font-size:15px;font-weight:600;background:var(--ab,var(--tile));
color:var(--af,var(--mute));user-select:none;overflow:hidden}
.av img{display:block;width:100%;height:100%;object-fit:cover}
.av.im{background:var(--tile);box-shadow:0 0 0 1px var(--line)}
.bd{flex:1;min-width:0}
.name{font-size:13px;font-weight:600;color:var(--af,var(--ink));line-height:20px}
.m{position:relative;padding:2px 0}
.m+.m{margin-top:4px}
time{float:right;margin-left:12px;color:var(--mute);font-size:12px;line-height:20px;
font-variant-numeric:tabular-nums}
.name+time{line-height:20px}
.txt{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.txt a{color:var(--link);text-decoration:none}
/* markdown 正文. 尺寸一律用 em 跟着气泡走 —— 这是聊天记录里的一段话,
不是一篇文档, 标题再大也不该盖过说话人的名字 */
.md{white-space:normal}
.md>:first-child{margin-top:0}
.md>:last-child{margin-bottom:0}
.md p,.md ul,.md ol,.md pre,.md blockquote,.md table{margin:.4em 0}
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{margin:.7em 0 .3em;font-size:1em;
font-weight:600;line-height:1.35}
.md h1{font-size:1.18em}
.md h2{font-size:1.08em}
.md ul,.md ol{padding-left:1.35em}
.md blockquote{padding-left:.8em;border-left:3px solid var(--line);color:var(--mute)}
.md code{padding:.1em .35em;border-radius:4px;background:var(--tile);font-size:.9em;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.md pre{padding:8px 10px;border-radius:8px;background:var(--tile);overflow:auto;
white-space:pre}
.md pre code{padding:0;background:none}
.md hr{border:0;border-top:1px solid var(--line);margin:.7em 0}
.md table{display:block;overflow:auto;border-collapse:collapse;font-size:.95em}
.md th,.md td{padding:4px 8px;border:1px solid var(--line);text-align:left}
.at{color:var(--link)}
.pics{display:grid;gap:3px;margin:6px 0 2px}
.pics.c2{grid-template-columns:repeat(2,minmax(0,1fr))}
.pics.c3{grid-template-columns:repeat(3,minmax(0,1fr))}
.pics a{display:block;min-width:0;border-radius:10px;overflow:hidden;background:var(--tile)
center/cover no-repeat;box-shadow:inset 0 0 0 1px var(--line)}
.pics a:focus-visible{outline:2px solid var(--link);outline-offset:2px}
.pics img{display:block;width:100%;height:100%;object-fit:cover;aspect-ratio:1}
.pics.c1{grid-template-columns:minmax(0,1fr)}
.pics.c1 a{width:fit-content;max-width:100%;min-width:96px;min-height:96px}
.pics.c1 img{width:auto;max-width:100%;height:auto;aspect-ratio:auto;object-fit:fill;
background:var(--tile)}
video,audio{display:block;width:100%;max-height:62vh;border-radius:10px;margin:6px 0 2px;
background:#000}
video:not(.ld){aspect-ratio:16/9}
audio{background:none}
.file{display:inline-flex;gap:8px;align-items:center;margin:6px 0 2px;padding:9px 12px;
background:var(--tile);border-radius:10px;color:inherit;text-decoration:none;font-size:14px;
overflow-wrap:anywhere}
.gone{display:flex;align-items:center;justify-content:center;margin:6px 0 2px;height:84px;
border-radius:10px;color:var(--mute);font-size:13px;background:repeating-linear-gradient(
135deg,var(--tile) 0 8px,var(--hatch) 8px 9px)}
.pics .gone{margin:0;height:auto;aspect-ratio:1}
.pics.c1 .gone{aspect-ratio:auto;height:84px}
.nest{display:inline-block;margin:6px 0 2px;padding:6px 10px;border-radius:10px;
background:var(--tile);color:var(--mute);font-size:13px}
details.nest{display:block;padding:0;color:inherit;font-size:14px}
.nest>summary{padding:8px 12px;cursor:pointer;list-style:none;color:var(--mute);font-size:13px;
font-variant-numeric:tabular-nums}
.nest>summary::-webkit-details-marker{display:none}
.nest>summary::before{content:"▸ "}
.nest[open]>summary::before{content:"▾ "}
.nb{padding:0 6px 6px}
.nb .g{padding:8px 6px;gap:8px}
.nb .av{width:28px;height:28px;font-size:13px}
.ft{padding:20px 16px 28px;color:var(--mute);font-size:12px;text-align:center;
font-variant-numeric:tabular-nums}
.empty{padding:44px 16px;text-align:center;color:var(--mute)}
.lb{position:fixed;inset:0;z-index:9;background:#000;display:flex;align-items:center;
justify-content:center;touch-action:none;overflow:hidden}
.lb[hidden]{display:none}
.lb img{max-width:100vw;max-height:100vh;object-fit:contain;user-select:none;
-webkit-user-drag:none;transition:transform .16s ease-out;will-change:transform}
.lb .cnt{position:fixed;top:14px;left:0;right:0;
text-align:center;color:#9a9a9e;font-size:13px;margin:0;pointer-events:none}
.lb button{position:fixed;z-index:1;border:0;background:rgba(0,0,0,.45);color:#f2f2f7;
font:20px/1 system-ui;width:40px;height:40px;border-radius:20px;cursor:pointer}
.lb button:focus-visible{outline:2px solid #fff}
.lb .x{top:10px;right:12px}
.lb .p,.lb .n{top:50%;margin-top:-20px}
.lb .p{left:10px}.lb .n{right:10px}
.lb.one .p,.lb.one .n{display:none}
.lbopen{overflow:hidden}
@media(prefers-reduced-motion:reduce){.lb img{transition:none}}
#g-bg{position:fixed;inset:0;z-index:0;pointer-events:none;display:block}
.gclip{position:absolute;top:0;left:0;width:100%;overflow:hidden;z-index:0;pointer-events:none}
#g-sc{position:absolute;top:0;left:0;display:block;will-change:transform}
html.gl body{background:transparent}
html.gl .top{--bar:rgba(255,255,255,.42)}
html.gl .sheet{position:relative;z-index:1;background:transparent;margin:8px auto 0;border-radius:18px}
@media(max-width:700px){html.gl .sheet{margin:8px 8px 0}}
html.gl .ft{position:relative;z-index:1}
html.gl .av,html.gl .file,html.gl .nest,html.gl .day,html.gl .gone{background:transparent}
/* 玻璃上不放实色块, 改用一圈描边: 代码块那点底色在磨砂背景上会变成一块脏斑 */
html.gl .md code,html.gl .md pre{background:transparent;
box-shadow:inset 0 0 0 1px var(--line)}
/* 代码块里那个 <code> 是跨行的行内盒: 描边会按每一行各画一个框, 里外两层框套着
看着像表格. 外面 <pre> 那一圈就够了 */
html.gl .md pre code{box-shadow:none}
html.gl .av.im{background:var(--tile)}
html.gl .gone{background:repeating-linear-gradient(135deg,transparent 0 8px,var(--line) 8px 9px)}
@media(prefers-color-scheme:dark){html.gl .top{--bar:rgba(16,16,20,.42)}}
/* 画布版画不了时的兜底: 极光底(上面 body 那条, 色相和画布版同一批)+ 一块按同一
色相染过的面板. 面板**不透明**, 也**不上 backdrop-filter** —— 它是整页消息列表,
几千像素高, 这两样都要求浏览器把这么高一块单独合成, 安卓上实测会塌(Edge 底色画
不满, QQ 内置浏览器糊成一块灰). 而且半透明/模糊在这儿本来就看不出来: 面板背后
只有一层平滑渐变. 色相留在面板自己的底色里, 每份记录的身份色照样在. */
html:not(.gl) .sheet{background:var(--pane);margin:8px auto 0;border-radius:18px;
box-shadow:var(--gsh)}
@media(max-width:700px){html:not(.gl) .sheet{margin:8px 8px 0}}
/* 顶栏反过来: 高度固定, 且确实有正文从底下滚过去, 糊是刚需, 实测也没问题.
只有真支持模糊时才敢把它调淡, 否则维持原来那层几乎不透明的底. */
@supports ((-webkit-backdrop-filter:blur(1px)) or (backdrop-filter:blur(1px))){
html:not(.gl) .top{--bar:var(--bar2)}}
""".strip()

# 灯箱: 滑动/方向键切图, 双击或双指放大, 点背景或 × 关闭(点图不关, 免得误关双击).
# touchmove 一律 preventDefault: iOS 上 overflow:hidden 拦不住底层页面滚动.
# CSP 哈希在 import 时按内容计算, 改脚本无需手动更新.
_SCRIPT = """
(function(){var L=[].slice.call(document.querySelectorAll('a.img'));if(!L.length)return;
var lb=document.getElementById('lb'),im=lb.querySelector('img'),c=lb.querySelector('.cnt'),
i=0,s=1,bs=1,tx=0,ty=0,tap=0,t0=null,d0=0,s0=1,mv=false,ld=null,drag=null,bk=0;
if(L.length<2)lb.className+=' one';
function ap(){im.style.transform='translate('+tx+'px,'+ty+'px) scale('+s+')'}
function reset(){s=1;bs=1;tx=ty=0;im.style.width=im.style.height=im.style.maxWidth=im.style.maxHeight='';ap()}
function clamp(){var mx=Math.max(0,(im.offsetWidth*s-lb.clientWidth)/2),
my=Math.max(0,(im.offsetHeight*s-lb.clientHeight)/2);
tx=Math.min(mx,Math.max(-mx,tx));ty=Math.min(my,Math.max(-my,ty))}
function zoom(ns,x,y){ns=Math.min(8/bs,Math.max(1/bs,ns));var cx=lb.clientWidth/2,cy=lb.clientHeight/2,k=ns/s;
tx=(x-cx)-k*((x-cx)-tx);ty=(y-cy)-k*((y-cy)-ty);s=ns;clamp();ap()}
function bake(){clearTimeout(bk);if(s===1)return;if(Math.abs(bs*s-1)<.01)return reset();
var w=im.offsetWidth*s;bs*=s;s=1;im.style.transition='none';im.style.maxWidth=im.style.maxHeight='none';
im.style.width=w+'px';im.style.height='auto';ap();requestAnimationFrame(function(){im.style.transition=''})}
function later(){clearTimeout(bk);bk=setTimeout(bake,200)}
function toggle(x,y){if(s>1||bs>1)reset();else{zoom(Math.max(2.5,lb.clientWidth/im.offsetWidth),x,y);later()}}
function go(n){i=(n+L.length)%L.length;reset();var a=L[i],full=a.href,med=a.getAttribute('data-m')||full;
im.src=med;c.textContent=(i+1)+' / '+L.length;lb.hidden=false;document.body.classList.add('lbopen');
ld=null;if(med!==full){var pre=new Image();ld=pre;
pre.onload=function(){if(ld===pre&&!lb.hidden)im.src=full};pre.src=full}}
function off(){lb.hidden=true;ld=null;im.removeAttribute('src');document.body.classList.remove('lbopen');L[i].focus()}
L.forEach(function(a,n){a.addEventListener('click',function(e){e.preventDefault();go(n)})});
lb.addEventListener('click',function(e){var b=e.target.closest('button');
if(b){b.className==='p'?go(i-1):b.className==='n'?go(i+1):off();return}
if(e.target!==im&&!mv)off()});
im.addEventListener('dblclick',function(e){e.preventDefault();toggle(e.clientX,e.clientY)});
lb.addEventListener('wheel',function(e){e.preventDefault();zoom(s*(e.deltaY<0?1.2:1/1.2),e.clientX,e.clientY);later()},{passive:false});
im.addEventListener('mousedown',function(e){if(s*bs>1){drag={x:e.clientX,y:e.clientY,tx:tx,ty:ty};e.preventDefault()}});
window.addEventListener('mousemove',function(e){if(!drag)return;im.style.transition='none';
tx=drag.tx+e.clientX-drag.x;ty=drag.ty+e.clientY-drag.y;clamp();ap();mv=true});
window.addEventListener('mouseup',function(){drag=null;im.style.transition='';setTimeout(function(){mv=false},0)});
document.addEventListener('keydown',function(e){if(lb.hidden)return;
if(e.key==='Escape')off();else if(e.key==='ArrowLeft')go(i-1);else if(e.key==='ArrowRight')go(i+1)});
function dist(t){var dx=t[0].clientX-t[1].clientX,dy=t[0].clientY-t[1].clientY;return Math.sqrt(dx*dx+dy*dy)}
lb.addEventListener('touchstart',function(e){var t=e.touches;mv=false;im.style.transition='none';
if(t.length===2){d0=dist(t);s0=s;t0=null}else t0={x:t[0].clientX,y:t[0].clientY,tx:tx,ty:ty}},{passive:true});
lb.addEventListener('touchmove',function(e){var t=e.touches;e.preventDefault();
if(t.length===2){zoom(s0*dist(t)/d0,(t[0].clientX+t[1].clientX)/2,(t[0].clientY+t[1].clientY)/2);mv=true}
else if(s*bs>1&&t0){tx=t0.tx+t[0].clientX-t0.x;ty=t0.ty+t[0].clientY-t0.y;clamp();ap();mv=true}},{passive:false});
lb.addEventListener('touchend',function(e){im.style.transition='';if(e.touches.length)return;
if(s!==1)bake();var ch=e.changedTouches[0];
if(t0&&s*bs===1&&!mv){var dx=ch.clientX-t0.x;if(Math.abs(dx)>48){mv=true;go(dx<0?i+1:i-1);t0=null;return}}
if(!mv&&e.target===im){var now=Date.now();if(now-tap<320){e.preventDefault();toggle(ch.clientX,ch.clientY);tap=0}else tap=now}
t0=null});
})();
""".strip()

_LB_SCRIPT = _SCRIPT

# 液态玻璃 WebGL2 层(不可用时退回 CSS 版, 极光与面板在样式里有对应).
# - 两张画布: #g-bg 固定视口画背景极光; #g-sc 透明画玻璃, 放在文档流里随滚动由合成器搬运
#   (iOS 滚动在合成线程, fixed 画布上的玻璃会拖在 DOM 后面).
# - 矩形存 RGBA32F 数据纹理, 每帧只刷视口一条带; 开 preserveDrawingBuffer, 否则漏画一帧就露断层.
# - 掉帧自动关并记 24h; 减少动效/省流/无 WebGL2/上下文丢失也退回 CSS.
# - html.gl 时 DOM 全透明, 画错必须立刻退回. 安卓 QQ 内置浏览器会静默渲染失败(离屏 FBO
#   未初始化, 整块发灰), 故检查 checkFramebufferStatus + 首帧验收覆盖与颜色 + 异常即退.
#   关闭原因记在 window.__gl, ?diag=1 可见; ?gl=0 强制 CSS, ?gl=1 跳过偏好与掉帧记忆.
# - QQ CDN 无 CORS, 图片进不了纹理; 极光色相取自说话人(body[data-hues]).
_GL_SCRIPT = r"""
(function(){var W=window,B=document.body,H=document.documentElement,hues=(B.getAttribute('data-hues')||'').split(',').map(Number).filter(function(x){return x===x});
function no(w){W.__gl=w}
if(hues.length<2)return no('nohue');if(!W.WebGL2RenderingContext)return no('nowebgl2');
var FORCE=/[?&]gl=1/.test(location.search);
if(/[?&]gl=0/.test(location.search))return no('url');
try{if(!FORCE){if(matchMedia('(prefers-reduced-motion: reduce)').matches)return no('motion');
if(navigator.connection&&navigator.connection.saveData)return no('savedata');
if(Date.now()-(+localStorage.getItem('fg-off')||0)<864e5)return no('memo')}}catch(e){}
var VS='#version 300 es\nin vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
var BG='uniform vec2 uRes;uniform float uTime;uniform int uDark;uniform vec4 uHue;uniform int uNH;'+
'vec3 hsl(float h,float s,float l){vec3 k=mod(vec3(0.,8.,4.)+h/30.,12.);float a=s*min(l,1.-l);return l-a*clamp(min(k-3.,9.-k),-1.,1.);}'+
'vec3 blob(vec3 c,vec2 p,vec2 o,float R,vec3 rgb,float a){float w=pow(clamp(1.-length(p-o)/R,0.,1.),1.6);return mix(c,rgb,a*w);}'+
'vec3 bgColor(vec2 p){float t=clamp(dot(p-uRes*.5,vec2(.7071))/((uRes.x+uRes.y)*.7071)+.5,0.,1.);'+
'vec3 c=uDark==1?mix(vec3(.05,.055,.07),vec3(.085,.09,.11),t):mix(vec3(1.),vec3(.945,.953,.97),t);'+
'float vw=uRes.x,vh=uRes.y,amp=uDark==1?.24:.20,L=uDark==1?.42:.72;'+
'vec2 P[4]=vec2[4](vec2(.08,.05),vec2(.94,.30),vec2(.14,.85),vec2(.80,.98));'+
'for(int i=0;i<4;i++){if(i>=uNH)break;float k=.5-.5*cos(uTime*6.2831853/(34.+6.*float(i)));'+
'vec2 o=P[i]*vec2(vw,vh)+vec2(.06*vw,.05*vh)*(k*2.-1.);float R=(.44*vw+120.)*(1.+.08*k);'+
'c=blob(c,p,o,R,hsl(uHue[i],.6,L),amp);}return c;}';
var FBG='#version 300 es\nprecision mediump float;uniform vec2 uSize;'+BG+'out vec4 o;void main(){vec2 p=vec2(gl_FragCoord.x,uSize.y-gl_FragCoord.y)/uSize*uRes;o=vec4(bgColor(p),1.);}';
var FBL='#version 300 es\nprecision mediump float;uniform sampler2D tMap;uniform vec2 uSize;uniform vec2 uDir;out vec4 o;'+
'void main(){vec2 uv=gl_FragCoord.xy/uSize;vec2 d=uDir/uSize;vec3 c=texture(tMap,uv).rgb*.227027;'+
'c+=(texture(tMap,uv+d).rgb+texture(tMap,uv-d).rgb)*.1945946;c+=(texture(tMap,uv+d*2.).rgb+texture(tMap,uv-d*2.).rgb)*.1216216;'+
'c+=(texture(tMap,uv+d*3.).rgb+texture(tMap,uv-d*3.).rgb)*.0540541;c+=(texture(tMap,uv+d*4.).rgb+texture(tMap,uv-d*4.).rgb)*.0162162;o=vec4(c,1.);}';
var FM='#version 300 es\nprecision highp float;uniform vec2 uSize;uniform float uDpr;uniform sampler2D tBlur;uniform vec2 uPointer;uniform vec2 uView;'+
'uniform highp sampler2D tRects;uniform int uN1;uniform int uN2;'+BG+'out vec4 o;'+
'vec4 rectAt(int i){return texelFetch(tRects,ivec2(i,0),0);}vec4 parAt(int i){return texelFetch(tRects,ivec2(i,1),0);}vec4 radAt(int i){return texelFetch(tRects,ivec2(i,2),0);}'+
'float pickR(vec4 r,vec2 p){float t=p.x<0.?r.x:r.y;float b=p.x<0.?r.w:r.z;return p.y<0.?t:b;}'+
'float sdRB(vec2 p,vec2 b,vec4 rr,out vec2 g){float r=pickR(rr,p);vec2 q=abs(p)-b+r;vec2 m=max(q,0.);float d=length(m)+min(max(q.x,q.y),0.)-r;'+
'if(q.x>0.||q.y>0.)g=normalize(m+1e-5)*sign(p);else g=(q.x>q.y)?vec2(sign(p.x),0.):vec2(0.,sign(p.y));return d;}'+
'vec3 sb(vec2 p){return texture(tBlur,clamp(vec2(p.x/uRes.x,1.-p.y/uRes.y),vec2(.002),vec2(.998))).rgb;}'+
'vec3 th(vec2 p){vec2 v=p+uView;v.y=clamp(v.y,0.,uRes.y);return mix(bgColor(v),sb(v),.55);}'+
'vec3 rf(vec2 p,vec2 d){vec3 c;c.r=th(p+d*.92).r;c.g=th(p+d).g;c.b=th(p+d*1.08).b;float l=dot(c,vec3(.2126,.7152,.0722));return clamp(mix(vec3(l),c,1.15)*(uDark==1?1.:1.03),0.,1.);}'+
'vec2 rim(vec2 g,float d,float ew,float k){float e=1.-smoothstep(0.,ew,-d);return -g*(pow(e,3.)+1.4*pow(e,14.))*k;}'+
'vec2 lens(vec2 p,vec4 r,float m){vec2 c=r.xy+r.zw*.5;return c+(p-c)*m;}'+
'vec3 sw(vec3 c,float a){return c+a*(1.-c);}'+
'vec2 lp(){return uPointer.x<-1e4?vec2(uRes.x*.22,-uRes.y*.45)-uView:uPointer;}'+
'vec3 surf(vec3 c,vec2 p,vec4 r,float d,vec2 g,float ew,float tint,float top,float hov,float lw){'+
'float hl=uDark==1?.55:1.;c=uDark==1?mix(c,vec3(.10,.11,.13),tint*2.4):mix(c,vec3(1.),tint);'+
'float qy=(p.y-r.y)/r.w;c=mix(c,vec3(1.),top*hl*(1.-smoothstep(0.,.4,qy)));'+
'vec2 L=normalize(lp()-p);float lit=clamp(dot(g,L),0.,1.);'+
'float line=smoothstep(.6*lw,1.5*lw,-d)*(1.-smoothstep(2.*lw,3.6*lw,-d));c=sw(c,line*(.26+.6*lit)*hl);'+
'float fr=pow(1.-smoothstep(0.,ew,-d),3.);c=sw(c,fr*(.07+.09*lit)*hl);'+
'if(hov>.5){float a=1.-smoothstep(0.,180.,length(p-uPointer));c=sw(c,a*a*.16);}return c;}'+
'float hash(vec2 p){return fract(sin(dot(p,vec2(12.9898,78.233)))*43758.5453);}'+
'void main(){vec2 px=vec2(gl_FragCoord.x,uSize.y-gl_FragCoord.y)/uDpr;'+
'float d1=1e9;vec2 g1=vec2(0.);vec4 r1=vec4(0.),p1=vec4(0.);'+
'for(int i=0;i<4;i++){if(i>=uN1)break;vec4 r=rectAt(i);vec2 g;float d=sdRB(px-r.xy-r.zw*.5,r.zw*.5,radAt(i),g);if(d<d1){d1=d;g1=g;r1=r;p1=parAt(i);}}'+
'float d2=1e9;vec2 g2=vec2(0.);vec4 r2=vec4(0.),p2=vec4(0.);'+
'for(int i=0;i<128;i++){if(i>=uN2)break;int j=4+i;vec4 r=rectAt(j);vec2 g;float d=sdRB(px-r.xy-r.zw*.5,r.zw*.5,radAt(j),g);if(d<d2){d2=d;g2=g;r2=r;p2=parAt(j);}}'+
'bool i1=d1<0.&&p1.x>.002,i2=d2<0.&&p2.x>.002;'+
'if(!(i1||i2)){o=vec4(0.);return;}'+
'vec3 c=bgColor(px+uView);vec2 D1=rim(g1,d1,40.,12.);'+
'if(i1){vec3 s=surf(rf(lens(px,r1,.975),D1),px,r1,d1,g1,40.,.14,.05,p1.y,1.);c=mix(c,s,p1.x);}'+
'if(i2){vec2 D2=rim(g2,d2,12.,3.);vec2 pp=lens(px,r2,.97)+D2;vec3 u;'+
'if(i1){u=surf(rf(lens(pp,r1,.975),D1),pp,r1,d1,g1,40.,.14,.05,0.,1.);u=mix(bgColor(pp+uView),u,p1.x);}else u=rf(lens(px,r2,.97),D2);'+
'vec3 s=surf(u,px,r2,d2,g2,12.,i1?.10:.14,.05,p2.y,.65);c=mix(c,s,p2.x);}'+
'c+=(hash(gl_FragCoord.xy)-.5)/255.;o=vec4(c,1.);}';
var TW=132,RD=new Float32Array(TW*3*4);
function mk(cv,opaque){var gl=cv.getContext('webgl2',{alpha:!opaque,depth:false,stencil:false,antialias:false,premultipliedAlpha:!opaque,preserveDrawingBuffer:!opaque,powerPreference:'high-performance'});if(!gl)return null;
function sh(t,s){var x=gl.createShader(t);gl.shaderSource(x,s);gl.compileShader(x);if(!gl.getShaderParameter(x,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(x));return x}
function pr(fs){var p=gl.createProgram();gl.attachShader(p,sh(gl.VERTEX_SHADER,VS));gl.attachShader(p,sh(gl.FRAGMENT_SHADER,fs));gl.linkProgram(p);
if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(p));var u={},n=gl.getProgramParameter(p,gl.ACTIVE_UNIFORMS);
for(var i=0;i<n;i++){var a=gl.getActiveUniform(p,i);u[a.name.replace('[0]','')]=gl.getUniformLocation(p,a.name)}return{p:p,u:u,a:gl.getAttribLocation(p,'p')}}
var buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
function tex(f){var t=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,t);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,f);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,f);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);return t}
function rt(){var t=tex(gl.LINEAR),f=gl.createFramebuffer();gl.bindFramebuffer(gl.FRAMEBUFFER,f);gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,t,0);return{t:t,f:f,w:8,h:8}}
var S={gl:gl,cv:cv,opaque:opaque,bg:pr(FBG),bl:pr(FBL),mn:opaque?null:pr(FM),a:rt(),b:rt(),rt:tex(gl.NEAREST)};
gl.bindTexture(gl.TEXTURE_2D,S.rt);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA32F,TW,3,0,gl.RGBA,gl.FLOAT,null);
S.use=function(P){gl.useProgram(P.p);gl.bindBuffer(gl.ARRAY_BUFFER,buf);gl.enableVertexAttribArray(P.a);gl.vertexAttribPointer(P.a,2,gl.FLOAT,false,0,0)};
S.size=function(w,h,vw,vh,dpr){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);cv.style.width=w+'px';cv.style.height=h+'px';
var lw=Math.max(8,Math.round(vw*dpr/3)),lh=Math.max(8,Math.round(vh*dpr/3));[S.a,S.b].forEach(function(r){gl.bindTexture(gl.TEXTURE_2D,r.t);
gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,lw,lh,0,gl.RGBA,gl.UNSIGNED_BYTE,null);r.w=lw;r.h=lh});S.vw=vw;S.vh=vh;S.dpr=dpr;S.w=w;S.h=h};
return S}
var bg=document.createElement('canvas'),clip=document.createElement('div'),sc=document.createElement('canvas');
bg.id='g-bg';clip.className='gclip';sc.id='g-sc';clip.appendChild(sc);
var bgS,scS;try{bgS=mk(bg,true);scS=mk(sc,false)}catch(e){return no('init '+e)}
if(!bgS||!scS)return no('noctx');
B.insertBefore(clip,B.firstChild);B.insertBefore(bg,B.firstChild);H.classList.add('gl');
var dark=false,mq=null;try{mq=matchMedia('(prefers-color-scheme: dark)');dark=mq.matches;mq.onchange=function(e){dark=e.matches;full=true;wake()}}catch(e){}
var coarse=matchMedia('(pointer: coarse)').matches,vw=0,vh=0,dpr=1,margin=96,canH=0,y0=0,clipH=-1,full=true,lastFull=0;
var pt=[-1e5,-1e5],T=0,last=0,act=0,raf=0,t0=0,perf=[],strikes=0,dead=false,shown=false;
var shells=[],ctls=[];
function radii(el){var cs=getComputedStyle(el),w=el.offsetWidth;function f(v){var n=parseFloat(v);if(n!==n)return 0;return /%\s*$/.test(v)?w*n/100:n}
return[f(cs.borderTopLeftRadius),f(cs.borderTopRightRadius),f(cs.borderBottomRightRadius),f(cs.borderBottomLeftRadius)]}
function grab(sel){return[].map.call(document.querySelectorAll(sel),function(el){return{el:el,rad:radii(el),hv:0,rc:null}})}
function collect(){shells=grab('.sheet').slice(0,4);ctls=grab('.av:not(.im),.file,.nest,.day,.gone')}
collect();
function setBg(u){var gl=this.gl;gl.uniform2f(u.uRes,vw,vh);gl.uniform1f(u.uTime,T);gl.uniform1i(u.uDark,dark?1:0);gl.uniform4f(u.uHue,hues[0],hues[1],hues[2]||0,hues[3]||0);gl.uniform1i(u.uNH,Math.min(4,hues.length))}
function put(j,r,hv,rad,g){var o=j*4;RD[o]=r.x-g;RD[o+1]=r.y-g;RD[o+2]=r.w+2*g;RD[o+3]=r.h+2*g;o=(TW+j)*4;RD[o]=1;RD[o+1]=hv;RD[o+2]=0;RD[o+3]=0;
o=(2*TW+j)*4;RD[o]=rad[0]+g;RD[o+1]=rad[1]+g;RD[o+2]=rad[2]+g;RD[o+3]=rad[3]+g}
function write(ry0,ry1){var n1=0,n2=0,i,p,r;
for(i=0;i<shells.length&&n1<4;i++){p=shells[i];r=p.rc;if(!r||r.y+r.h+16<=ry0||r.y-16>=ry1)continue;put(n1++,r,0,p.rad,0)}
for(i=0;i<ctls.length&&n2<128;i++){p=ctls[i];r=p.rc;if(!r||r.y+r.h+16<=ry0||r.y-16>=ry1)continue;var g=3*p.hv;put(4+n2++,r,p.hv>.5?1:0,p.rad,g)}
var gl=scS.gl;gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D,scS.rt);gl.texSubImage2D(gl.TEXTURE_2D,0,0,0,TW,3,gl.RGBA,gl.FLOAT,RD);return[n1,n2]}
function blurBg(S){var gl=S.gl;gl.disable(gl.SCISSOR_TEST);gl.bindFramebuffer(gl.FRAMEBUFFER,S.a.f);gl.viewport(0,0,S.a.w,S.a.h);S.use(S.bg);setBg.call(S,S.bg.u);gl.uniform2f(S.bg.u.uSize,S.a.w,S.a.h);gl.drawArrays(gl.TRIANGLES,0,3);
S.use(S.bl);gl.uniform2f(S.bl.u.uSize,S.a.w,S.a.h);gl.uniform1i(S.bl.u.tMap,0);gl.activeTexture(gl.TEXTURE0);
for(var i=0;i<2;i++){gl.bindFramebuffer(gl.FRAMEBUFFER,S.b.f);gl.bindTexture(gl.TEXTURE_2D,S.a.t);gl.uniform2f(S.bl.u.uDir,1,0);gl.drawArrays(gl.TRIANGLES,0,3);
gl.bindFramebuffer(gl.FRAMEBUFFER,S.a.f);gl.bindTexture(gl.TEXTURE_2D,S.b.t);gl.uniform2f(S.bl.u.uDir,0,1);gl.drawArrays(gl.TRIANGLES,0,3)}}
function drawBg(){var S=bgS,gl=S.gl;gl.bindFramebuffer(gl.FRAMEBUFFER,null);gl.viewport(0,0,S.cv.width,S.cv.height);S.use(S.bg);setBg.call(S,S.bg.u);gl.uniform2f(S.bg.u.uSize,S.cv.width,S.cv.height);gl.drawArrays(gl.TRIANGLES,0,3)}
function drawSc(ox,oy,regions){var S=scS,gl=S.gl;blurBg(S);gl.bindFramebuffer(gl.FRAMEBUFFER,null);gl.viewport(0,0,S.cv.width,S.cv.height);S.use(S.mn);var u=S.mn.u;setBg.call(S,u);
gl.uniform2f(u.uSize,S.cv.width,S.cv.height);gl.uniform1f(u.uDpr,dpr);gl.uniform2f(u.uPointer,pt[0]-ox,pt[1]-oy);gl.uniform2f(u.uView,ox,oy);
gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,S.a.t);gl.uniform1i(u.tBlur,0);gl.uniform1i(u.tRects,1);
gl.enable(gl.SCISSOR_TEST);var W=S.cv.width;
for(var k=0;k<regions.length;k++){var a=Math.max(0,Math.floor(regions[k][0])),b=Math.min(S.h,Math.ceil(regions[k][1]));if(b<=a)continue;
gl.scissor(0,Math.round((S.h-b)*dpr),W,Math.round((b-a)*dpr));gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);
var n=write(a,b);if(!n[0]&&!n[1])continue;gl.uniform1i(u.uN1,n[0]);gl.uniform1i(u.uN2,n[1]);gl.drawArrays(gl.TRIANGLES,0,3)}
gl.disable(gl.SCISSOR_TEST)}
function resize(){vw=innerWidth;vh=innerHeight;dpr=Math.min(devicePixelRatio||1,coarse?1:1.5);margin=coarse?vh:96;canH=vh+2*margin;
bgS.size(vw,vh,vw,vh,dpr);scS.size(vw,canH,vw,vh,dpr);full=true}
function place(sy){var docH=H.scrollHeight;if(clipH!==docH){clipH=docH;clip.style.height=docH+'px'}
var want=Math.max(0,Math.min(sy-margin,docH-canH));if(full||Math.abs(want-y0)>margin*.5){y0=want;sc.style.transform='translateY('+y0+'px)';full=true}}
function rtOk(S){var gl=S.gl,i,r;for(i=0;i<2;i++){r=i?S.b:S.a;gl.bindFramebuffer(gl.FRAMEBUFFER,r.f);
if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)return false}
gl.bindFramebuffer(gl.FRAMEBUFFER,null);return true}
function ok(ox,oy){var S=scS,gl=S.gl,r=shells[0]&&shells[0].rc;
if(!r||r.w<12||r.h<12||r.y<20)return 0;
var W=S.cv.width,y=Math.round((S.h-r.y+12)*dpr);if(y<0||y>=S.cv.height)return 0;
var row=new Uint8Array(W*4);gl.readPixels(0,y,W,1,gl.RGBA,gl.UNSIGNED_BYTE,row);
for(var i=3;i<row.length;i+=4)if(row[i]>8)return -1;
var cx=r.x+r.w*.5,cy=r.y+10,x=Math.round(cx*dpr);y=Math.round((S.h-cy)*dpr);
if(x<0||y<0||x>=W||y>=S.cv.height)return 0;
var a=new Uint8Array(4);gl.readPixels(x,y,1,1,gl.RGBA,gl.UNSIGNED_BYTE,a);
if(a[3]<=8)return -1;
var bx=Math.round((cx+ox)*dpr),by=Math.round((vh-cy-oy)*dpr),b=new Uint8Array(4);
if(bx<0||by<0||bx>=bgS.cv.width||by>=bgS.cv.height)return 1;
bgS.gl.readPixels(bx,by,1,1,bgS.gl.RGBA,bgS.gl.UNSIGNED_BYTE,b);
return Math.abs(a[0]-b[0])+Math.abs(a[1]-b[1])+Math.abs(a[2]-b[2])<150?1:-1}
function frame(now){try{draw(now)}catch(e){kill('throw')}}
function draw(now){raf=0;if(dead)return;var dt=last?now-last:16;last=now;
if(document.hidden||B.classList.contains('lbopen'))return;
if(innerWidth!==vw||innerHeight!==vh)resize();
T+=Math.min(dt,50)/1000;var sy=scrollY;place(sy);
var o=sc.getBoundingClientRect(),ox=o.left,oy=o.top,i,p,r;
for(i=0;i<shells.length;i++){p=shells[i];r=p.el.getBoundingClientRect();p.rc={x:r.left-ox,y:r.top-oy,w:r.width,h:r.height}}
var lo=sy-y0-margin,hi=lo+canH+2*margin;
for(i=0;i<ctls.length;i++){p=ctls[i];r=p.el.getBoundingClientRect();var y=r.top-oy;if(y+r.height<lo||y>hi){p.rc=null;continue}
p.rc={x:r.left-ox,y:y,w:r.width,h:r.height};var h=pt[0]>=r.left&&pt[0]<=r.right&&pt[1]>=r.top&&pt[1]<=r.bottom?1:0;p.hv+=(h-p.hv)*.25;if(p.hv<.01)p.hv=0}
drawBg();
var regions;if(full||now-lastFull>1000){regions=[[0,canH]];lastFull=now;full=false}else{var b0=sy-y0;regions=[[b0-vh*.1,b0+vh*1.1]]}
drawSc(ox,oy,regions);
if(!shown){var v=ok(ox,oy);if(v<0)return kill('paint');if(v>0){shown=true;W.__gl='on'}}
if(!t0)t0=now;if(now-t0>3000){perf.push(dt);if(perf.length>240)perf.shift();
if(perf.length>=240&&perf.length%60===0){var s=perf.slice().sort(function(a,b){return a-b}),slow=perf.filter(function(x){return x>50}).length/perf.length;
if(slow>=.75||s[120]>55){if(++strikes>=3)return kill('slow')}else strikes=0}}
if(now-act<1500)raf=requestAnimationFrame(frame)}
function wake(){act=performance.now();if(!raf&&!dead){last=0;raf=requestAnimationFrame(frame)}}
function kill(why){if(dead)return;dead=true;W.__gl=why;if(raf)cancelAnimationFrame(raf);H.classList.remove('gl');bg.remove();clip.remove();
[bgS,scS].forEach(function(S){try{var x=S.gl.getExtension('WEBGL_lose_context');if(x)x.loseContext()}catch(e){}});
if(why==='slow')try{localStorage.setItem('fg-off',String(Date.now()))}catch(e){}}
[bg,sc].forEach(function(c){c.addEventListener('webglcontextlost',function(e){e.preventDefault();kill('lost')})});
addEventListener('scroll',wake,{passive:true});addEventListener('resize',wake);addEventListener('load',wake);
addEventListener('pointermove',function(e){pt=[e.clientX,e.clientY];wake()},{passive:true});
addEventListener('pointerup',function(e){if(e.pointerType==='touch')pt=[-1e5,-1e5];wake()},{passive:true});
document.addEventListener('pointerleave',function(){pt=[-1e5,-1e5];wake()});
document.addEventListener('visibilitychange',wake);
if(W.MutationObserver)new MutationObserver(function(){if(!B.classList.contains('lbopen'))wake()}).observe(B,{attributes:true,attributeFilter:['class']});
if(window.ResizeObserver){var ro=new ResizeObserver(function(){full=true;wake()});shells.forEach(function(p){ro.observe(p.el)})}
resize();
if(!rtOk(bgS)||!rtOk(scS))return kill('rt');
wake();
})();
""".strip()

# 视频元数据排队加载: 离视口 300px 内, 最多 2 并发, 15s 超时; 拿到后换成真实比例
_VID_SCRIPT = r"""
(function(){var V=[].slice.call(document.querySelectorAll('video[preload=none]'));if(!V.length||!window.IntersectionObserver)return;
var want=[],busy=0,MAX=2;
function pump(){while(busy<MAX&&want.length){(function(v){if(v.dataset.ld)return;v.dataset.ld=1;busy++;var done=false,
t=setTimeout(fin,15000);function fin(){if(done)return;done=true;busy--;clearTimeout(t);v.classList.add('ld');pump()}
v.addEventListener('loadedmetadata',fin,{once:true});v.addEventListener('error',fin,{once:true});
v.preload='metadata';v.load()})(want.shift())}}
var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){io.unobserve(e.target);want.push(e.target)}});
want.sort(function(a,b){return a.compareDocumentPosition(b)&4?-1:1});pump()},{rootMargin:'300px 0px'});
V.forEach(function(v){io.observe(v)});
})();
""".strip()

_SCRIPT = _LB_SCRIPT + "\n" + _VID_SCRIPT + "\n" + _GL_SCRIPT

# 诊断页(/f/<任意 token>?diag=1): 打印浏览器能力并摆几块只差一个属性的板子, 截图定位问题.
# 只在诊断页拼入, CSP 哈希单算.
_DIAG_SCRIPT = r"""
(function(){var H=document.documentElement,cs=getComputedStyle,o=[];
function p(k,v){o.push(k+': '+v)}
function q(s){return document.querySelector(s)}
function bf(e){return cs(e).backdropFilter||cs(e).webkitBackdropFilter||'-'}
p('ua',navigator.userAgent);
p('vp',innerWidth+'x'+innerHeight+' dpr'+devicePixelRatio+' doc'+H.scrollHeight);
p('html.class','"'+H.className+'" __gl='+(window.__gl||'?'));
var t=q('.top'),s=q('.sheet');
p('top','pad'+cs(t).paddingTop+' '+cs(t).backgroundColor+' bf='+bf(t));
p('sheet',cs(s).backgroundColor+' r='+cs(s).borderTopLeftRadius+' of='+cs(s).overflow+
' h='+Math.round(s.getBoundingClientRect().height)+' bf='+bf(s));
p('--pane',cs(H).getPropertyValue('--pane')+' --h1='+cs(H).getPropertyValue('--h1'));
p('body bg',(cs(document.body).backgroundImage||'-').slice(0,64));
var S=window.CSS&&CSS.supports;
p('supports',['inset|0','backdrop-filter|blur(1px)','color|hsl(212 60% 72% / .2)','width|70vw']
.map(function(d){var a=d.split('|');return a[0]+'='+(S?CSS.supports(a[0],a[1]):'?')}).join(' '));
try{var c=document.createElement('canvas'),g=c.getContext('webgl2');
if(!g)p('webgl2','no');else{var e=g.getExtension('WEBGL_debug_renderer_info');
p('webgl2',(e?g.getParameter(e.UNMASKED_RENDERER_WEBGL):g.getParameter(g.RENDERER))+
' maxTex'+g.getParameter(g.MAX_TEXTURE_SIZE)+
' hp'+g.getShaderPrecisionFormat(g.FRAGMENT_SHADER,g.HIGH_FLOAT).precision)}}catch(e){p('webgl2','err '+e)}
var out=q('#dgout');out.textContent=o.join('\n');
setTimeout(function(){
o.push('2s: html.class "'+H.className+'" __gl='+(window.__gl||'?'));
o.push('sheet2: '+cs(s).backgroundColor+' bf='+bf(s));
out.textContent=o.join('\n');
// 顺手回传一份: 出问题的机器在别人手里, 靠截图来回猜太慢, 直接进服务端日志.
try{fetch(location.pathname+'?diag=report',{method:'POST',body:o.join('\n').slice(0,4000)})
.then(function(){out.textContent+='\n[已回传服务端]'},
function(e){out.textContent+='\n[回传失败 '+e+']'})}catch(e){out.textContent+='\n[回传异常 '+e+']'}
},2000);
})();
""".strip()

_DIAG_BODY = (
    '<header class="top"><div class="bar"><h1>聊天记录</h1>'
    '<span class="cnt">诊断</span></div></header>'
    "<style>"
    ".dgo{max-width:680px;margin:8px auto 0;padding:12px;border-radius:14px;"
    "background:var(--pane);font:11px/1.5 ui-monospace,Menlo,Consolas,monospace;"
    "white-space:pre-wrap;word-break:break-all;color:var(--ink)}"
    ".dgb{max-width:680px;margin:10px auto 0;padding:10px 14px;border-radius:18px;"
    "font-size:13px;height:900px;box-shadow:var(--gsh)}"
    "@media(max-width:700px){.dgo,.dgb{margin-left:8px;margin-right:8px}}"
    ".dgA,.dgE{background:var(--pane)}.dgE{height:4000px}"
    ".dgB,.dgC{background:rgba(127,127,127,.35)}.dgC{overflow:hidden}"
    ".dgD{background:var(--pane);-webkit-backdrop-filter:blur(20px);"
    "backdrop-filter:blur(20px)}"
    "</style>"
    '<pre class="dgo" id="dgout">采集中…</pre>'
    '<main class="sheet"><div class="g"><div class="av a0">诊</div><div class="bd">'
    '<div class="name">诊断</div><p class="txt">这块是真正的 .sheet, '
    "样式跟正常页面一模一样.</p></div></div></main>"
    '<div class="dgb dgA">A 不透明(正式版现在用的)</div>'
    '<div class="dgb dgB">B 半透明</div>'
    '<div class="dgb dgC">C 半透明 + overflow:hidden</div>'
    '<div class="dgb dgD">D 不透明 + backdrop-filter</div>'
    '<div class="dgb dgE">E 不透明, 但高 4000px —— 单测高度</div>'
    '<footer class="ft">诊断页 · 往下滚, 看哪块的底色画不到底或者糊了</footer>'
)


def _csp(script: str) -> str:
    """只放行指定哈希的脚本; img/media 放开 https:(QQ CDN 与任意外站)."""
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    return ("default-src 'none'; img-src https: data:; media-src https:; "
            "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors 'none'; script-src 'sha256-{digest}'")


# 卡顿实测(?perf=1 才拼入): 滚动时采帧间隔, 停手 1.2s 后 POST 回日志;
# 顺带用 <img> naturalWidth 探 CDN 支持哪些 spec 尺寸.
_PERF_SCRIPT = r"""
(function(){var W=window,N=navigator,d=[],raf=0,last=0,t=0,sent=false,imgs=[],probes=[];
function q(s){return [].slice.call(document.querySelectorAll(s))}
function loop(now){if(last)d.push(now-last);last=now;
if(performance.now()-t<1200)raf=requestAnimationFrame(loop);else{raf=0;last=0;report()}}
addEventListener('scroll',function(){t=performance.now();
if(!raf){last=0;raf=requestAnimationFrame(loop)}},{passive:true});
function pct(a,f){return Math.round(a[Math.min(a.length-1,Math.floor(a.length*f))]*10)/10}
function report(){if(sent||d.length<45)return;sent=true;
var s=d.slice().sort(function(a,b){return a-b}),o=[];
o.push('ua: '+N.userAgent);
o.push('vp: '+innerWidth+'x'+innerHeight+' dpr'+devicePixelRatio+' doc'+document.documentElement.scrollHeight);
o.push('dev: mem'+(N.deviceMemory||'?')+'GB cores'+(N.hardwareConcurrency||'?')+' __gl='+(W.__gl||'?'));
o.push('frames: n'+d.length+' p50='+pct(s,.5)+' p90='+pct(s,.9)+' p99='+pct(s,.99)+
' max='+Math.round(s[s.length-1])+' >32ms='+d.filter(function(x){return x>32}).length+
' >100ms='+d.filter(function(x){return x>100}).length);
q('.pics img').forEach(function(i){if(i.naturalWidth)imgs.push(i.naturalWidth+'x'+i.naturalHeight+
'@'+Math.round(i.getBoundingClientRect().width*devicePixelRatio))});
o.push('imgs('+q('.pics img').length+' 解出/显示宽): '+imgs.slice(0,10).join(' '));
o.push('probe: '+probes.join(' '));
try{fetch(location.pathname+'?diag=report',{method:'POST',body:o.join('\n').slice(0,4000)})}catch(e){}}
var a=q('a.img')[0];
if(a&&/spec=-?\d+/.test(a.href)){[198,720,1080,1440,2560].forEach(function(sp){
var im=new Image();im.onload=function(){probes.push(sp+'='+im.naturalWidth+'x'+im.naturalHeight)};
im.onerror=function(){probes.push(sp+'=err')};
im.src=a.href.replace(/([?&])spec=-?\d+/,'$1spec='+sp)})}
})();
""".strip()

_DIAG_FULL = _SCRIPT + "\n" + _DIAG_SCRIPT
_PERF_FULL = _SCRIPT + "\n" + _PERF_SCRIPT
CSP = _csp(_SCRIPT)
# 诊断/实测页需 connect-src 'self' 以 POST 回数据
def _csp_report(script: str) -> str:
    return _csp(script).replace("default-src 'none';",
                                "default-src 'none'; connect-src 'self';")


CSP_DIAG = _csp_report(_DIAG_FULL)
CSP_PERF = _csp_report(_PERF_FULL)


def _esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def _safe_url(url: object) -> str:
    text = str(url or "").strip()
    return text if text.startswith(_OK_SCHEMES) else ""


def _css_url(url: str) -> str:
    """style 属性里的 url(): 引号/括号/空白百分号编码, 防注入."""
    safe = "".join(f"%{ord(ch):02X}" if ch in "\"'()\\ \t\n" else ch for ch in url)
    return f'url("{safe}")'


def _variant(url: str, spec: int) -> str:
    """QQ CDN 直链的尺寸变体; 不适用时返回空串."""
    if not url.startswith(_QQ_CDN) or not _SPEC_RE.search(url):
        return ""
    return _SPEC_RE.sub(rf"\g<1>spec={spec}", url, count=1)


def _animated(kind: str, data: dict) -> bool:
    """动图(mface 或 .gif)不能用变体, 会被压成单帧."""
    if kind == "mface":
        return True
    hint = str(data.get("file") or data.get("name") or data.get("url") or "").lower()
    return hint.split("?", 1)[0].endswith(".gif")


def _hue_index(name: str) -> int:
    return hashlib.sha1(name.encode("utf-8")).digest()[0] % len(_HUES)


def _hue_style() -> str:
    light = "".join(f".a{i}{{--ab:hsl({h} 60% 91%);--af:hsl({h} 45% 38%)}}"
                    for i, h in enumerate(_HUES))
    dark = "".join(f".a{i}{{--ab:hsl({h} 28% 24%);--af:hsl({h} 55% 74%)}}"
                   for i, h in enumerate(_HUES))
    return f"{light}@media(prefers-color-scheme:dark){{{dark}}}"


def _autolink(escaped: str) -> str:
    def repl(match: re.Match) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in _URL_TAIL:
            tail = url[-1] + tail
            url = url[:-1]
        return (f'<a href="{url}" rel="noreferrer noopener" target="_blank">{url}</a>'
                + tail)
    return _URL_RE.sub(repl, escaped)


def _md_body(text: str) -> str:
    """markdown 正文, 复用 textimg 渲染器(已保证转义与只认 http(s)), 外链补 rel/target."""
    return md_to_html(text).replace(
        '<a href="', '<a target="_blank" rel="noreferrer noopener" href="')


def normalize_nodes(raw: list) -> list[dict]:
    """OneBot 各种 node 形状(含入站存库的 sender/message 形) -> [{name, time, segments}]."""
    nodes: list[dict] = []
    # 丢节点说明上游形状没识别, 必须留日志
    dropped: list[str] = []
    for item in raw or []:
        if not isinstance(item, dict):
            dropped.append(f"item={type(item).__name__}")
            continue
        data = item.get("data") if item.get("type") in ("node", "forward") else None
        data = data if isinstance(data, dict) else item
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        segments = data.get("content")
        if segments is None:
            segments = data.get("message")
        if isinstance(segments, str):
            segments = [{"type": "text", "data": {"text": segments}}]
        elif isinstance(segments, dict):
            # 裸 MessageSegment 会被编成单个对象
            segments = [segments]
        if not isinstance(segments, list):
            dropped.append(f"content={type(segments).__name__} keys={sorted(data)[:6]}")
            continue
        name = (data.get("nickname") or data.get("name") or data.get("uin")
                or sender.get("nickname") or sender.get("card") or "")
        try:
            stamp = int(data.get("time") or 0)
        except (TypeError, ValueError):
            stamp = 0
        try:
            uid = int(data.get("user_id") or data.get("uin") or sender.get("user_id") or 0)
        except (TypeError, ValueError):
            uid = 0
        # uid 供 sender 反查头像, 落库前换成 avatar URL
        nodes.append({"name": str(name), "time": stamp, "uid": uid,
                      "avatar": str(data.get("avatar") or ""),
                      "segments": [s for s in segments if isinstance(s, dict)]})
    if dropped:
        logger.warning("合并转发丢弃 %d/%d 个节点(上游形状没吃下来): %s",
                       len(dropped), len(raw or []), "; ".join(dropped[:5]))
    return nodes


def _count_media(segments: list, counts: dict[str, int]) -> None:
    """统计媒体数, 含嵌套记录."""
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type", "text")
        if kind in _MEDIA_TYPES:
            counts[kind] = counts.get(kind, 0) + 1
        elif kind == "node":
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if isinstance(data.get("content"), list):
                _count_media(data["content"], counts)


def summarize(nodes: list[dict]) -> str:
    """一行摘要, 如 "12 条消息 · 5 图片 · 1 视频"."""
    counts: dict[str, int] = {}
    for node in nodes:
        _count_media(node["segments"], counts)
    parts = [f"{len(nodes)} 条消息"]
    for kind in _MEDIA_TYPES:
        if counts.get(kind):
            parts.append(f"{counts[kind]} {_KIND_LABEL[kind]}")
    return " · ".join(parts)


def page_token(appid: str, nodes: list[dict]) -> str:
    """内容哈希作 token: 多群广播只建一页, 且不可推测."""
    blob = json.dumps([appid, nodes], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


class _Media:
    """本次渲染的媒体地址解析(见模块头), 并统计已过期的数量."""

    def __init__(self, created_ts: int, now: int, rkeys: dict[str, str]):
        self.created_ts = created_ts
        self.now = now
        self.rkeys = rkeys
        self.expired = 0

    def resolve(self, data: dict) -> tuple[str, str]:
        """-> (地址, 状态), 状态为 ok / missing / expired."""
        url = _safe_url(data.get("url") or data.get("file"))
        raw = _safe_url(data.get("raw_url"))
        if not url and not raw:
            return "", "missing"
        if url and cdnkeys.is_cdn(url):
            rkey = self.rkeys.get(cdnkeys.appid_of(url))
            if rkey:
                return cdnkeys.with_rkey(url, rkey), "ok"
            fresh_page = self.now - int(self.created_ts or 0) < MEDIA_TTL_SECONDS
            url = url if fresh_page and "rkey=" in url else ""
        if url:
            return url, "ok"
        try:
            until = int(data.get("raw_until") or 0)
        except (TypeError, ValueError):
            until = 0
        if raw and self.now < until:
            return raw, "ok"
        self.expired += 1
        return "", "expired"


def _render_pics(pics: list[tuple[str, str, bool]]) -> str:
    """相邻图片排成一格: 1 张满栏不限高, 2/4 张两列, 其余三列.

    pics 项为 (url, 状态, 可用变体); 可用变体的放中图垫小图, 否则用原图.
    """
    cols = 1 if len(pics) == 1 else 2 if len(pics) in (2, 4) else 3
    cells: list[str] = []
    for url, state, resizable in pics:
        if state == "missing":
            cells.append('<div class="gone">图片 · 地址缺失</div>')
            continue
        if state == "expired":
            cells.append('<div class="gone">图片 · 已过期</div>')
            continue
        med = _variant(url, _SPEC_MEDIUM) if resizable else ""
        thumb = _variant(url, _SPEC_THUMB) if resizable else ""
        attrs = f' data-m="{_esc(med)}"' if med else ""
        if med and cols == 1:
            # 满栏单图: 高倍屏直接取原图, 720 中图分辨率不够
            attrs += f' srcset="{_esc(med)} 1x, {_esc(url)} 2x"'
        # url("…") 进 style 属性需再 HTML 转义
        style = f' style="background-image:{_esc(_css_url(thumb))}"' if thumb else ""
        # decoding=async: 大图解码不阻塞滚动
        cells.append(f'<a class="img" href="{_esc(url)}"{attrs}{style}>'
                     f'<img src="{_esc(med or url)}" alt="" loading="lazy" '
                     f'decoding="async"></a>')
    return f'<div class="pics c{cols}">{"".join(cells)}</div>'


def _render_media(kind: str, data: dict, media: _Media) -> str:
    url, state = media.resolve(data)
    name = _esc(data.get("name") or data.get("file_name") or "")
    label = _KIND_LABEL.get(kind, kind)
    if kind == "file" and state != "ok":
        # 文件无直链, 只显示名字
        return f'<span class="file">📎 <span>{name or label}</span></span>'
    if state == "missing":
        return f'<div class="gone">{label} · 地址缺失</div>'
    if state == "expired":
        return f'<div class="gone">{label} · 已过期</div>'
    if kind == "video":
        # preload=none: 避免多视频占满同主机连接, 元数据由 _VID_SCRIPT 排队加载
        return f'<video src="{_esc(url)}" controls playsinline preload="none"></video>'
    if kind == "record":
        return f'<audio src="{_esc(url)}" controls preload="none"></audio>'
    return (f'<a class="file" href="{_esc(url)}" rel="noreferrer noopener">'
            f'📎 <span>{name or label}</span></a>')


def _render_segments(segments: list[dict], media: _Media, depth: int = 0) -> str:
    """节点内容 -> HTML; 相邻图片并成一格, 相邻 node 段并成可折叠的嵌套记录."""
    out: list[str] = []
    text_buf: list[str] = []
    pic_buf: list[tuple[str, str, bool]] = []
    nest_buf: list[dict] = []

    def flush_text() -> None:
        text = "".join(text_buf).strip()
        text_buf.clear()
        if not text:
            return
        # 探测口径与出站 msg_type=2 一致; 非 markdown 走纯文本(pre-wrap)
        if looks_like_markdown(text):
            out.append(f'<div class="txt md">{_md_body(text)}</div>')
        else:
            out.append(f'<p class="txt">{_autolink(_esc(text))}</p>')

    def flush_pics() -> None:
        if pic_buf:
            out.append(_render_pics(list(pic_buf)))
            pic_buf.clear()

    def flush_nest() -> None:
        if nest_buf:
            out.append(_render_nested(list(nest_buf), media, depth + 1))
            nest_buf.clear()

    for seg in segments:
        kind = seg.get("type", "text")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind in ("image", "mface"):
            flush_text()
            flush_nest()
            pic_buf.append((*media.resolve(data), not _animated(kind, data)))
            continue
        flush_pics()
        if kind == "node":
            flush_text()
            nest_buf.append(seg)
            continue
        flush_nest()
        if kind == "text":
            text_buf.append(str(data.get("text", "")))
        elif kind == "at":
            flush_text()
            out.append(f'<span class="at">@{_esc(data.get("name") or data.get("qq"))}</span> ')
        elif kind == "face":
            text_buf.append(f"[{data.get('summary') or '表情'}]")
        elif kind in _MEDIA_TYPES:
            flush_text()
            out.append(_render_media(kind, data, media))
        elif kind == "forward":
            # 未能展开的转发 id, 只显示胶囊
            flush_text()
            out.append('<span class="nest">聊天记录</span>')
        # reply 等其它段: 跳过
    flush_text()
    flush_pics()
    flush_nest()
    return "".join(out)


def _render_nested(segs: list[dict], media: _Media, depth: int) -> str:
    """嵌套转发: 默认收起; 超过 NEST_MAX_DEPTH 只显示胶囊."""
    inner = normalize_nodes(segs)
    if not inner or depth > NEST_MAX_DEPTH:
        return '<span class="nest">聊天记录</span>'
    return (f'<details class="nest"><summary>{_esc(summarize(inner))}</summary>'
            f'<div class="nb">{"".join(_render_groups(inner, media, depth))}</div></details>')


def _group_by_speaker(nodes: list[dict]) -> list[tuple[str, list[dict]]]:
    """连续同一人的消息并成一组."""
    groups: list[tuple[str, list[dict]]] = []
    for node in nodes:
        if groups and node["name"] and groups[-1][0] == node["name"]:
            groups[-1][1].append(node)
        else:
            groups.append((node["name"], [node]))
    return groups


def _render_groups(nodes: list[dict], media: _Media, depth: int) -> list[str]:
    """节点 -> 按说话人分组的 section; 嵌套记录(depth>0)不标日期."""
    parts: list[str] = []
    day = ""
    for name, members in _group_by_speaker(nodes):
        # 换天时标日期, 每条只显示时:分; 时间戳为 0 不显示
        first = members[0]["time"]
        if first and depth == 0:
            this_day = time.strftime("%m月%d日", time.localtime(first))
            if this_day != day:
                day = this_day
                parts.append(f'<div class="day">{day}</div>')
        rows: list[str] = []
        for node in members:
            stamp = (f'<time>{time.strftime("%H:%M", time.localtime(node["time"]))}</time>'
                     if node["time"] else "")
            rows.append(f'<div class="m">{stamp}'
                        f'{_render_segments(node["segments"], media, depth)}</div>')
        pic = _safe_url(members[0].get("avatar"))
        hue = f" a{_hue_index(name)}" if name else ""
        head = f'<div class="name">{_esc(name)}</div>' if name else ""
        # 真头像用 .im 标记, 不进玻璃层
        if pic:
            avatar = (f'<div class="av im"><img src="{_esc(pic)}" alt="" '
                      f'loading="lazy" decoding="async"></div>')
        elif name:
            avatar = f'<div class="av">{_esc(name.strip()[:1])}</div>'
        else:
            avatar = '<div class="av">·</div>'
        parts.append(f'<section class="g{hue}">{avatar}<div class="bd">{head}'
                     f'{"".join(rows)}</div></section>')
    return parts


def _page_hues(nodes: list[dict]) -> list[int]:
    """极光色相: 每个说话人一个(最多 4), 不足 2 个用默认色补."""
    hues: list[int] = []
    for node in nodes:
        if node["name"]:
            hue = _HUES[_hue_index(node["name"])]
            if hue not in hues:
                hues.append(hue)
        if len(hues) >= 4:
            break
    for hue in (212, 44, 160):
        if len(hues) >= 2:
            break
        if hue not in hues:
            hues.append(hue)
    return hues


def _shell(body: str, script: str = "", hues: list[int] | None = None) -> str:
    attr = f' data-hues="{",".join(str(h) for h in hues)}"' if hues else ""
    # CSS 兜底版的极光色相(三团, 不足循环取). 必须写在 :root 上: --pane 在 :root 上引用它们
    hvars = (":root{%s}" % "".join(f"--h{i + 1}:{hues[i % len(hues)]};" for i in range(3))
             if hues else "")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        # 不开 viewport-fit=cover, 页面无需铺到刘海下
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>聊天记录</title>"
        f"<style>{_STYLE}{_hue_style()}{hvars}</style></head><body{attr}>{body}"
        + (f"<script>{script}</script>" if script else "")
        + "</body></html>"
    )


def render_page(nodes: list[dict], created_ts: int, now: int | None = None,
                perf: bool = False, rkeys: dict[str, str] | None = None) -> str:
    """整页 HTML. rkeys: 换进直链的 {appid: rkey}."""
    now = int(time.time()) if now is None else now
    media = _Media(int(created_ts or 0), now, rkeys or {})
    parts = _render_groups(nodes, media, 0)
    if media.expired:
        # 页顶统一说明一次过期
        parts.insert(0, '<div class="day">部分图片/视频已过期</div>')
    made = time.strftime("%Y-%m-%d %H:%M", time.localtime(created_ts or now))
    body = (
        '<header class="top"><div class="bar"><h1>聊天记录</h1>'
        f'<span class="cnt">{_esc(summarize(nodes))}</span></div></header>'
        f'<main class="sheet">{"".join(parts)}</main>'
        f'<footer class="ft">{made} 生成</footer>'
        '<div class="lb" id="lb" hidden><p class="cnt"></p><img alt="" draggable="false">'
        '<button class="p" aria-label="上一张">‹</button>'
        '<button class="n" aria-label="下一张">›</button>'
        '<button class="x" aria-label="关闭">×</button></div>'
    )
    return _shell(body, script=_PERF_FULL if perf else _SCRIPT,
                  hues=_page_hues(nodes))


NOT_FOUND_HTML = _shell(
    '<header class="top"><div class="bar"><h1>聊天记录</h1></div></header>'
    '<main class="sheet"><div class="empty">这份聊天记录不存在, 或已经清理<br>'
    "回 QQ 看原消息</div></main>"
)

# 固定色相, 便于对比诊断截图
DIAG_HTML = _shell(_DIAG_BODY, script=_DIAG_FULL, hues=[212, 44])
