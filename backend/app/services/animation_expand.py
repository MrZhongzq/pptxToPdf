"""按 p:timing 把逐步出现的内容拆成多页。

用户报的问题：「graph 也没正确处理 p:timing，动画页没有自动分页，还是
堆叠的」——一页上按点击逐条出现的要点，转成 PDF 后全部叠在同一页，
做笔记时既看不清层次也没法对着讲课节奏标注。

做法是在**转换前**改写 pptx：解析每页的动画序列，为每次点击生成一份
该页的副本，副本里把「这一步之后才出现的内容」删掉。一页变 N 页，
之后的转换、切片、书签全部按新页数走。

### 只处理进入动画

`presetClass="entr"` 是「元素出现」的唯一成因，也正是用户抱怨的根因。
退出动画若也处理，需要表达「先出现后消失」，页数会爆炸且语义含糊；
强调与路径动画不改变可见性，对分页没有意义。它们一律忽略——不是漏了。

### 无法保证正确时不做，并明确告知

含 `mc:AlternateContent` 的页跳过：那里面有 mc:Choice/mc:Fallback 两套
并存的内容，ET 往返后哪一套生效由消费方决定，我们改写它等于赌。
跳过的页会记进 warnings 让用户看到，而不是假装展开成功了——这与
「绝不静默」是同一条要求。
"""

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from app.services.opc_rewrite import read_rels, rels_path

logger = logging.getLogger(__name__)

P_URI = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_URI = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_URI = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "{" + P_URI + "}"
A = "{" + A_URI + "}"
R = "{" + R_URI + "}"

PRESENTATION = "ppt/presentation.xml"
SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
SLIDE_REL_TYPE = R_URI + "/slide"

# 单页展开出的页数上限。做了 50 步动画的一页会把 59 页的 deck 变成上千页，
# 转换必然超时，而对做笔记也毫无用处——没人需要 50 张只差一行字的纸。
MAX_STEPS_PER_SLIDE = 20
# 整份 deck 展开后的总页数上限。同理，且这条还护着 Graph 通道的 100 页硬限。
MAX_TOTAL_PAGES = 500

# 会被当作「一个形状」的 spTree 直接子元素
SHAPE_TAGS = (f"{P}sp", f"{P}pic", f"{P}graphicFrame", f"{P}grpSp", f"{P}cxnSp")


@dataclass
class ExpandResult:
    expanded: bool
    pages_before: int
    pages_after: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Target:
    """一个进入动画的目标：整个形状，或形状里的一段文字。"""

    spid: str
    para_from: int | None = None
    para_to: int | None = None

    @property
    def is_paragraph(self) -> bool:
        return self.para_from is not None


def _register_namespaces() -> None:
    ET.register_namespace("p", P_URI)
    ET.register_namespace("a", A_URI)
    ET.register_namespace("r", R_URI)


def parse_steps(slide_root: ET.Element) -> list[list[Target]]:
    """解析出「每次点击各让哪些东西出现」。

    分步依据是 mainSeq 的 childTnLst 下的每个直接子 <p:par>——它正好
    对应用户的一次点击。一个 par 内部可能既有 clickEffect 又有
    withEffect（同时发生的动画），按 par 分组天然把它们归成一步，
    不需要单独判断 nodeType。
    """
    timing = slide_root.find(f"{P}timing")
    if timing is None:
        return []

    main_seq = None
    for seq in timing.iter(f"{P}seq"):
        ctn = seq.find(f"{P}cTn")
        if ctn is not None and ctn.get("nodeType") == "mainSeq":
            main_seq = ctn
            break
    if main_seq is None:
        return []

    child_lst = main_seq.find(f"{P}childTnLst")
    if child_lst is None:
        return []

    steps: list[list[Target]] = []
    for par in child_lst.findall(f"{P}par"):
        targets: list[Target] = []
        for ctn in par.iter(f"{P}cTn"):
            if ctn.get("presetClass") != "entr":
                continue
            for tgt_el in ctn.iter(f"{P}tgtEl"):
                sp_tgt = tgt_el.find(f"{P}spTgt")
                if sp_tgt is None:
                    continue
                spid = sp_tgt.get("spid")
                if not spid:
                    continue
                p_rg = sp_tgt.find(f"{P}txEl/{P}pRg")
                if p_rg is not None:
                    targets.append(
                        Target(spid, int(p_rg.get("st", 0)), int(p_rg.get("end", 0)))
                    )
                else:
                    targets.append(Target(spid))
        # 去重：同一个目标常被多条 cBhvr 重复引用（淡入 + 位移是两条）
        deduped = list(dict.fromkeys(targets))
        if deduped:
            steps.append(deduped)
    return steps


def prune_dead_steps(slide_root: ET.Element, steps: list[list[Target]]) -> list[list[Target]]:
    """剔除目标已经不存在的动画步骤。

    一个步骤如果它的目标形状不在 spTree 里、或段落索引全部越界，那么
    「隐藏它」与「不隐藏它」产出的页面**逐像素相同**——白白多一页，用户
    看到的是若干张一模一样的纸，只会以为转换出错了。

    这种情况真实存在的来源：形状在 PowerPoint 里被删掉但 p:timing 里的
    条目没跟着清（PowerPoint 自己不总是清理），以及我们在剥离媒体时删掉
    了空 txBody 的形状（见 media_strip 与 _hide 里那段说明）。

    用户那份 59 页课件实测 93 步、0 步无效——这次没踩到只是运气，防护
    不能建立在「样本恰好干净」上。
    """
    tree = slide_root.find(f"{P}cSld/{P}spTree")
    if tree is None:
        return steps

    present: dict[str, int] = {}
    for shape in tree:
        sid = _shape_id(shape)
        if sid is None:
            continue
        body = shape.find(f".//{P}txBody")
        present[sid] = len(body.findall(f"{A}p")) if body is not None else 0

    pruned: list[list[Target]] = []
    for step in steps:
        alive = [t for t in step if _target_exists(t, present)]
        if alive:
            pruned.append(alive)
    return pruned


def _target_exists(target: Target, present: dict[str, int]) -> bool:
    if target.spid not in present:
        return False
    if not target.is_paragraph:
        return True
    count = present[target.spid]
    return any(0 <= i < count for i in range(target.para_from, target.para_to + 1))


def _shape_id(shape: ET.Element) -> str | None:
    c_nv_pr = shape.find(f".//{P}cNvPr")
    return c_nv_pr.get("id") if c_nv_pr is not None else None


def _hide(slide_root: ET.Element, targets: list[Target]) -> None:
    """从这一版里删掉指定目标，使它们「还没出现」。"""
    sp_tree = slide_root.find(f"{P}cSld/{P}spTree")
    if sp_tree is None:
        return

    whole_shapes = {t.spid for t in targets if not t.is_paragraph}
    para_targets: dict[str, set[int]] = {}
    for t in targets:
        if t.is_paragraph:
            para_targets.setdefault(t.spid, set()).update(
                range(t.para_from, t.para_to + 1)
            )

    for shape in list(sp_tree):
        if shape.tag not in SHAPE_TAGS:
            continue
        sid = _shape_id(shape)
        if sid is None:
            continue
        if sid in whole_shapes:
            sp_tree.remove(shape)
            continue
        if sid in para_targets:
            if _would_empty_text_body(shape, para_targets[sid]):
                # 段落全被删光会留下一个空的 <p:txBody>，而 ECMA-376 要求
                # CT_TextBody 至少含一个 <a:p>。Office 在线服务据此拒绝整份
                # 文档（HTTP 406），LibreOffice 照转不误。既然这个形状这一步
                # 一个字都不该出现，直接整块删掉——既合法，语义也更准确。
                sp_tree.remove(shape)
            else:
                _drop_paragraphs(shape, para_targets[sid])


def _would_empty_text_body(shape: ET.Element, indices: set[int]) -> bool:
    """删掉这些段落之后，txBody 会不会一个 <a:p> 都不剩。"""
    tx_body = shape.find(f".//{P}txBody")
    if tx_body is None:
        return False
    total = len(tx_body.findall(f"{A}p"))
    if total == 0:
        return False
    return len([i for i in indices if 0 <= i < total]) >= total


def _drop_paragraphs(shape: ET.Element, indices: set[int]) -> None:
    """删掉形状里指定序号的段落。

    按原始索引删、且一次性删完：如果逐个删再重新编号，第二次删除的
    索引就会指向错位的段落。这里对每个版本都从原始 XML 重新开始，
    所以索引始终是原始的。
    """
    tx_body = shape.find(f".//{P}txBody")
    if tx_body is None:
        return
    paragraphs = tx_body.findall(f"{A}p")
    for idx in sorted(indices, reverse=True):
        if 0 <= idx < len(paragraphs):
            tx_body.remove(paragraphs[idx])


def _drop_timing(slide_root: ET.Element) -> None:
    """删掉 <p:timing>。

    展开之后每一版都是静态快照，动画信息已经没有意义。更要紧的是：
    timing 里的 <p:spTgt spid="N"/> 会引用被这一版删掉的形状，而 Office
    在线服务对这种「动画指向不存在的形状」零容忍，整份文档返回 406
    NotAcceptable——五期剥离媒体时踩过一模一样的坑（<p:video> 指向一个
    已经不再是媒体的形状），当时同样是 LibreOffice 照转不误、Graph 拒绝。

    原页（保留全部形状的那一版）本可以留着 timing，但同样删掉：留着它
    只会让 Office 按动画规则渲染，而那正是用户抱怨的「所有元素叠在
    一页上」。
    """
    timing = slide_root.find(f"{P}timing")
    if timing is not None:
        slide_root.remove(timing)


REL_NS_URI = "http://schemas.openxmlformats.org/package/2006/relationships"
NOTES_SLIDE_REL = R_URI + "/notesSlide"


def _strip_notes_rel(rels_blob: bytes) -> bytes:
    """从复制给新版本的 rels 里去掉 notesSlide 关系。

    这是真机二分实验抓到的 406 根因：备注页与幻灯片是**双向**引用——
    slide 的 rels 指向 notesSlideN，而 notesSlideN 的 rels 又指回它所属的
    那一个 slide。把原页的 rels 原样复制给新版本，就出现了「两个 slide
    指向同一个备注页，而备注页只认其中一个」的不一致，Office 在线服务
    据此拒绝整份文档（HTTP 406），LibreOffice 则照转不误。

    实验证据（真实课件 slide29，59 页）：
      A 未展开                    -> Graph 成功
      B 加一份完整副本（含 notes） -> Graph 406
      C 同 B 但去掉 notesSlide     -> Graph 成功

    展开出的中间版本本来也不需要备注页——备注内容归原页，那一版仍然
    完整保留着它。
    """
    ET.register_namespace("", REL_NS_URI)
    root = ET.fromstring(rels_blob)
    for rel in list(root):
        if rel.get("Type") == NOTES_SLIDE_REL:
            root.remove(rel)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")


def _has_alternate_content(raw: bytes) -> bool:
    return b"AlternateContent" in raw


def _slide_order(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """按 sldIdLst 顺序返回 [(rId, slide_part_name)]。"""
    root = ET.fromstring(zf.read(PRESENTATION))
    lst = root.find(f"{P}sldIdLst")
    if lst is None:
        raise ValueError("presentation.xml 缺少 sldIdLst")
    rid_to_target = {rid: target for rid, _t, target in read_rels(zf, PRESENTATION)}
    return [
        (sld.get(f"{R}id"), rid_to_target[sld.get(f"{R}id")])
        for sld in lst.findall(f"{P}sldId")
    ]


def _next_ids(zf: zipfile.ZipFile) -> tuple[int, int, int]:
    """返回可用的 (下一个 slideN 编号, 下一个 rId 数字, 下一个 sldId 数值)。"""
    max_slide = 0
    for name in zf.namelist():
        m = SLIDE_RE.match(name)
        if m:
            max_slide = max(max_slide, int(m.group(1)))

    max_rid = 0
    for rid, _t, _target in read_rels(zf, PRESENTATION):
        m = re.match(r"rId(\d+)$", rid or "")
        if m:
            max_rid = max(max_rid, int(m.group(1)))

    root = ET.fromstring(zf.read(PRESENTATION))
    lst = root.find(f"{P}sldIdLst")
    max_sld = 255
    if lst is not None:
        for sld in lst.findall(f"{P}sldId"):
            try:
                max_sld = max(max_sld, int(sld.get("id", "0")))
            except ValueError:
                continue
    return max_slide + 1, max_rid + 1, max_sld + 1


def expand_animations(src: Path) -> ExpandResult:
    """就地展开动画分步。没有可展开的动画时不重写文件。"""
    _register_namespaces()

    with zipfile.ZipFile(src) as zf:
        order = _slide_order(zf)
        pages_before = len(order)

        plans: dict[str, list[list[Target]]] = {}
        warnings: list[str] = []
        for page_no, (_rid, part) in enumerate(order, start=1):
            raw = zf.read(part)
            if _has_alternate_content(raw):
                warnings.append(
                    f"第 {page_no} 页含 AlternateContent（通常是 SmartArt 或墨迹），"
                    "未展开动画"
                )
                continue
            root = ET.fromstring(raw)
            steps = prune_dead_steps(root, parse_steps(root))
            if not steps:
                continue
            if len(steps) > MAX_STEPS_PER_SLIDE:
                warnings.append(
                    f"第 {page_no} 页有 {len(steps)} 个动画步骤，超过上限 "
                    f"{MAX_STEPS_PER_SLIDE}，未展开"
                )
                continue
            plans[part] = steps

        if not plans:
            return ExpandResult(False, pages_before, pages_before, warnings)

        extra = sum(len(s) for s in plans.values())
        if pages_before + extra > MAX_TOTAL_PAGES:
            warnings.append(
                f"展开后共 {pages_before + extra} 页，超过上限 {MAX_TOTAL_PAGES}，"
                "整份未展开"
            )
            return ExpandResult(False, pages_before, pages_before, warnings)

        payload = _build(zf, order, plans)

    src.write_bytes(payload.data)
    logger.info(
        "animations expanded path=%s %d -> %d pages warnings=%d",
        src.name, pages_before, payload.pages, len(warnings),
    )
    return ExpandResult(True, pages_before, payload.pages, warnings)


@dataclass
class _Built:
    data: bytes
    pages: int


def _build(
    zf: zipfile.ZipFile,
    order: list[tuple[str, str]],
    plans: dict[str, list[list[Target]]],
) -> _Built:
    """组装展开后的 pptx。

    新版本插在原页**之前**：原 slide 保留为「全部出现」的最后一版，
    这样它的 rels、媒体引用、备注关系全都不用动——新增的只是几份内容
    更少的副本。反过来做（原页当第一版）需要把原页的关系重新指向新
    part，凭空多一类可能出错的改写。
    """
    next_slide_no, next_rid_no, next_sld_id = _next_ids(zf)

    rewritten_originals: dict[str, bytes] = {}  # 原 part -> 去掉 timing 的正文
    new_parts: dict[str, bytes] = {}      # part 名 -> slide xml
    new_rels: dict[str, bytes] = {}       # part 名 -> 它的 rels（复制原页的）
    # 原 part -> 要插在它前面的 [(part_name, rId, sldId)]
    inserts: dict[str, list[tuple[str, str, int]]] = {}

    for _rid, part in order:
        steps = plans.get(part)
        if not steps:
            continue
        raw = zf.read(part)
        src_rels_name = rels_path(part)
        try:
            rels_blob = zf.read(src_rels_name)
        except KeyError:
            rels_blob = None

        # 原页作为「全部出现」的最后一版，同样去掉 timing——理由见 _drop_timing
        original_root = ET.fromstring(raw)
        _drop_timing(original_root)
        rewritten_originals[part] = ET.tostring(
            original_root, xml_declaration=True, encoding="UTF-8"
        )

        for k in range(len(steps)):
            # 第 k 版显示前 k 步，隐藏第 k 步及之后的所有目标
            hidden: list[Target] = []
            for later in steps[k:]:
                hidden.extend(later)

            root = ET.fromstring(raw)
            _hide(root, hidden)
            _drop_timing(root)
            new_name = f"ppt/slides/slide{next_slide_no}.xml"
            new_parts[new_name] = ET.tostring(root, xml_declaration=True, encoding="UTF-8")
            if rels_blob is not None:
                new_rels[rels_path(new_name)] = _strip_notes_rel(rels_blob)

            inserts.setdefault(part, []).append(
                (new_name, f"rId{next_rid_no}", next_sld_id)
            )
            next_slide_no += 1
            next_rid_no += 1
            next_sld_id += 1

    presentation = _rewrite_presentation(zf.read(PRESENTATION), order, inserts)
    pres_rels = _rewrite_presentation_rels(zf.read(rels_path(PRESENTATION)), inserts)
    content_types = _rewrite_content_types(zf.read("[Content_Types].xml"), new_parts.keys())

    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zf.infolist():
            name = item.filename
            if name == PRESENTATION:
                zout.writestr(item, presentation)
            elif name == rels_path(PRESENTATION):
                zout.writestr(item, pres_rels)
            elif name == "[Content_Types].xml":
                zout.writestr(item, content_types)
            elif name in rewritten_originals:
                zout.writestr(item, rewritten_originals[name])
            else:
                zout.writestr(item, zf.read(name))
        for name, blob in new_parts.items():
            zout.writestr(name, blob)
        for name, blob in new_rels.items():
            zout.writestr(name, blob)

    total = len(order) + sum(len(v) for v in inserts.values())
    return _Built(buf.getvalue(), total)


# <p:sldId .../> 的两种合法形式：自闭合，以及挂了 <p:extLst/> 子元素的
# 非自闭合。\b 保证不会误匹配到父元素 sldIdLst。与 pptx_split 里同款正则
# 同一个理由：presentation.xml 必须逐字节保留，不能用 ET 往返。
_SLD_ID_RE = re.compile(
    rb"<(\w+:)?sldId\b[^>]*?(?:/>|>.*?</(\w+:)?sldId\s*>)",
    re.DOTALL,
)


def _rewrite_presentation(
    raw: bytes,
    order: list[tuple[str, str]],
    inserts: dict[str, list[tuple[str, str, int]]],
) -> bytes:
    """在 sldIdLst 里把新版本插到对应原页之前。

    正则手术而不是 ET 往返：presentation.xml 可能带 mc:Ignorable，而 ET
    会丢掉「声明了但没用到」的 xmlns，导致 mc:Ignorable 指向一个不存在的
    前缀——三期在这上面修了一轮，教训不该重走。
    """
    if not inserts:
        return raw

    part_by_index = [part for _rid, part in order]
    idx = 0

    def repl(m: re.Match) -> bytes:
        nonlocal idx
        part = part_by_index[idx] if idx < len(part_by_index) else None
        idx += 1
        if part is None or part not in inserts:
            return m.group(0)
        prefix = b"".join(
            b'<p:sldId id="%d" r:id="%s"/>' % (sld_id, rid.encode())
            for _name, rid, sld_id in inserts[part]
        )
        return prefix + m.group(0)

    return _SLD_ID_RE.sub(repl, raw)


def _rewrite_presentation_rels(
    raw: bytes, inserts: dict[str, list[tuple[str, str, int]]]
) -> bytes:
    if not inserts:
        return raw
    additions = []
    for entries in inserts.values():
        for name, rid, _sld_id in entries:
            # presentation.xml 在 ppt/ 下，它的 rels 里 Target 相对 ppt/ 解析
            target = name.removeprefix("ppt/")
            additions.append(
                f'<Relationship Id="{rid}" Type="{SLIDE_REL_TYPE}" Target="{target}"/>'.encode()
            )
    return raw.replace(b"</Relationships>", b"".join(additions) + b"</Relationships>")


def _rewrite_content_types(raw: bytes, new_parts) -> bytes:
    additions = b"".join(
        f'<Override PartName="/{name}" ContentType="application/vnd.openxmlformats-'
        f'officedocument.presentationml.slide+xml"/>'.encode()
        for name in new_parts
    )
    return raw.replace(b"</Types>", additions + b"</Types>")
