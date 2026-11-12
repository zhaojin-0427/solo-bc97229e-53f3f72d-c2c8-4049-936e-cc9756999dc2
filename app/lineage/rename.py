"""源列改名迁移引擎（仅静态分析，不执行任何 SQL）。

输入：源表结构文本、若干具名视图 SQL、目标（源表, 旧列名, 新列名）。
输出：改写后的源表结构文本与每个视图的新 SQL，以及改名过程的说明。

核心约束（与需求逐条对应）：
- 只改写**实际绑定到目标源列**的 ColumnRef；同名字符串字面量、其他表/CTE 的
  同名列、被嵌套 CTE 遮蔽的引用均不动。
- 绑定解析规则与 analyzer 完全一致：限定名按别名/表名匹配，裸列名在多来源中
  歧义即报错（溯源分析本来也会拒绝该视图）。
- 表别名原样保留；星号在涉及目标源表的查询块中就地展开为显式列清单
  （允许的补别名/展开星号），只改目标列名，输出列顺序不变。
- CTE 输出列若由目标源列直通而来（含 UNION ALL 同位置直通），列名随源列变化，
  下游对该 CTE 列的引用同步改写；显式别名（含 CTE 列别名清单）保持下游接口不变。
- 新名与源表现有列冲突、任一视图解析/溯源失败，整批拒绝（不产生任何结果）。
- 改名前后由 analyzer 双向校验：输出列名（位置对齐）、顺序、值/条件来源、
  敏感标记、表达式文本（新名还原为旧名后）必须完全一致。
"""

import re

from . import ast_nodes as ast
from .analyzer import Analyzer, canonical_expr, collect_refs
from .errors import LineageError
from .parser import parse_sql
from .schema import parse_schemas
from .tokenizer import tokenize


# ---------------------------------------------------------------- 数据结构

class RNode:
    """改名后某个查询块的一个输出列（模拟 analyzer 解析后的列环境）。"""

    __slots__ = ("name", "origin", "renamed", "sensitive")

    def __init__(self, name, origin, renamed=False, sensitive=False):
        self.name = name              # 改名后的输出列名
        # origin: None（常量/复合表达式，不直通任一单列）
        #         或 (table_lower, col_lower)，直通某源列
        self.origin = origin
        self.renamed = renamed        # 直通目标源列、且名称发生了变化
        self.sensitive = sensitive


class RenamePlan:
    """单个视图的改名计划。"""

    def __init__(self, view_name, old_sql, new_sql, changed_refs,
                 expanded_stars, affected, notes):
        self.view_name = view_name
        self.old_sql = old_sql
        self.new_sql = new_sql
        self.changed_refs = changed_refs      # [{line, col, old, new, kind}]
        self.expanded_stars = expanded_stars  # [{line, col, columns}]
        self.affected = affected              # 该视图是否实际引用目标源列
        self.notes = notes                    # [str, ...]


# ---------------------------------------------------------------- 入口校验

def _is_ident(s):
    if not s:
        return False
    if not (s[0].isalpha() or s[0] == "_"):
        return False
    return all(c.isalnum() or c in ("_", "$") for c in s)


def check_conflicts(schema_text, table_name, old_name, new_name):
    """前置校验：目标表/旧列存在、新名合法且与源表现有列不冲突。"""
    if not _is_ident(new_name):
        raise LineageError(f"新列名 {new_name!r} 不是合法标识符（字母/下划线开头，"
                           "仅含字母、数字、下划线、$）")
    tables = parse_schemas(schema_text)
    tkey = table_name.lower()
    if tkey not in tables:
        raise LineageError(f"源表 {table_name} 不在源表结构中")
    spec = tables[tkey]
    old_col = spec.get(old_name)
    if old_col is None:
        raise LineageError(f"源表 {spec.name} 中不存在旧列 {old_name}")
    if new_name.lower() == old_name.lower():
        raise LineageError(f"新列名 {new_name} 与旧列名相同，无需改名")
    clash = spec.get(new_name)
    if clash is not None:
        raise LineageError(
            f"新列名冲突：源表 {spec.name} 已存在列 {clash.name}，"
            "改名会造成同表列名重复，整批拒绝")
    return tables, spec, old_col


# ---------------------------------------------------------------- 源表结构改写

def rename_schema_text(schema_text, table_name, old_name, new_name):
    """只替换目标表定义括号内的目标列标识符，保留注释、空白与敏感星号。"""
    out_lines = []
    target = table_name.lower()
    for raw in schema_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            out_lines.append(raw)
            continue
        body = line[:-1] if line.endswith(";") else line
        lparen = body.find("(")
        rparen = body.rfind(")")
        if lparen <= 0 or rparen != len(body) - 1:
            out_lines.append(raw)
            continue
        tname = body[:lparen].strip()
        if tname.lower() != target:
            out_lines.append(raw)
            continue
        # 在括号内逐列定位，仅替换与旧列精确匹配的那一段
        prefix = raw[:lparen + 1]
        suffix = raw[rparen:]
        middle = raw[lparen + 1:rparen]
        new_parts = []
        found = False
        for part in middle.split(","):
            stripped = part.strip()
            lead = part[:len(part) - len(part.lstrip())]
            tail = part[len(part.rstrip()):]
            star = ""
            core = stripped
            if core.endswith("*"):
                star = "*"
                core = core[:-1].strip()
            if core.lower() == old_name.lower() and not found:
                new_parts.append(f"{lead}{new_name}{star}{tail}")
                found = True
            else:
                new_parts.append(part)
        if not found:
            raise LineageError(f"源表 {table_name} 的结构文本中未找到列 {old_name}")
        out_lines.append(prefix + ",".join(new_parts) + suffix)
    if schema_text.endswith("\n"):
        return "\n".join(out_lines) + "\n"
    return "\n".join(out_lines)


# ---------------------------------------------------------------- 视图改名器

class _ViewRenamer:
    """对单个视图 AST 做绑定解析并收集 token 级改写。"""

    def __init__(self, query, sql, tables, target_table, old_name, new_name):
        self.query = query
        self.sql = sql
        self.tables = tables                       # parse_schemas 结果（旧结构）
        self.tkey = target_table.lower()
        self.old_lower = old_name.lower()
        self.new_name = new_name
        self.edits = []                            # [(start, end, replacement)]
        self.changed_refs = []
        self.expanded_stars = []
        self.affected = False
        self.notes = []
        self._line_offsets = self._build_offsets(sql)

    def _build_offsets(self, sql):
        offsets = [0]
        for ch in sql:
            offsets.append(offsets[-1] + 1 if ch != "\n" else 0)
        # offsets[line] 为该行首字符在全文中的偏移（line 从 1 开始）
        starts = [0]
        for i, ch in enumerate(sql):
            if ch == "\n":
                starts.append(i + 1)
        return starts

    def _span(self, line, col, length):
        start = self._line_offsets[line - 1] + (col - 1)
        return start, start + length

    def _span_at(self, line, col, raw_text):
        start = self._line_offsets[line - 1] + (col - 1)
        end = start + len(raw_text)
        if self.sql[start:end] != raw_text:
            raise LineageError("内部错误：token 定位失败，改名已中止")
        return start, end

    def _replace_token(self, token, new_text, kind, old_text=None):
        # 引用标识符（"x" / `x` / [x]）的 token 值不带引号，
        # 需按原始 SQL 中带引号的完整文本来定位跨度，并保留原引号风格
        start = self._line_offsets[token.line - 1] + (token.col - 1)
        raw = self.sql[start:start + len(token.value)]
        quoted = False
        if raw == token.value:
            raw_text = token.value
        else:
            quoted = True
            ch = self.sql[start]
            if ch == "[":
                raw_text = "[" + token.value + "]"
            else:
                raw_text = ch + token.value + ch
        start, end = self._span_at(token.line, token.col, raw_text)
        replacement = new_text
        if quoted:
            if raw_text[0] == "[":
                replacement = "[" + new_text + "]"
            else:
                replacement = raw_text[0] + new_text + raw_text[0]
        self.edits.append((start, end, replacement))
        self.changed_refs.append({
            "line": token.line, "col": token.col,
            "old": old_text or token.value, "new": new_text, "kind": kind,
        })

    # ---- 输出列环境计算 ----

    def _source_nodes(self, ref, cte_map):
        """解析 FROM/JOIN 的一个 TableRef，返回 (nodes, kind, table_lower)。"""
        key = ref.name.lower()
        if key in cte_map:
            return cte_map[key], "cte", None
        if key in self.tables:
            spec = self.tables[key]
            nodes = []
            for col in spec.columns:
                origin = (key, col.name.lower())
                renamed = origin == (self.tkey, self.old_lower)
                nodes.append(RNode(
                    self.new_name if renamed else col.name,
                    origin, renamed, col.sensitive))
            return nodes, "base", key
        raise LineageError(
            f"FROM 中的 {ref.name} 既不是已定义的 CTE，也不是已知源表",
            ref.line, ref.col)

    def _bind_sources(self, core, cte_map):
        if core.from_table is None:
            return []
        sources = []
        bindings = set()

        def bind(ref):
            nodes, kind, table_lower = self._source_nodes(ref, cte_map)
            binding = (ref.alias or ref.name).lower()
            if binding in bindings:
                raise LineageError(f"表别名 {binding} 在查询块中重复",
                                   ref.line, ref.col)
            bindings.add(binding)
            # 改名节点同时以新旧名建索引：旧 SQL 中的引用按旧名解析，
            # 已就地改写后的列名按新名解析（如 UNION 后续分支）
            by_name = {}
            for n in nodes:
                by_name.setdefault(n.name.lower(), n)
                if n.renamed:
                    by_name.setdefault(self.old_lower, n)
            sources.append({
                "binding": binding,
                "alias": ref.alias,
                "ref_name": ref.name,
                "kind": kind,
                "table": table_lower,
                "nodes": nodes,
                "by_name": by_name,
            })

        bind(core.from_table)
        for join in core.joins:
            bind(join.right)
        return sources

    def _resolve(self, ref, sources):
        """与 Analyzer._resolve_ref 同语义，返回 RNode。"""
        if ref.table is not None:
            key = ref.table.lower()
            for src in sources:
                if src["binding"] == key:
                    node = src["by_name"].get(ref.name.lower())
                    if node is None:
                        raise LineageError(
                            f"{ref.table}.{ref.name} 字段不存在",
                            ref.line, ref.col)
                    return src, node
            raise LineageError(
                f"限定名 {ref.table}. 找不到对应的表或别名", ref.line, ref.col)
        matches = []
        for src in sources:
            node = src["by_name"].get(ref.name.lower())
            if node is not None:
                matches.append((src, node))
        if not matches:
            raise LineageError(
                f"字段 {ref.name} 在当前查询块的任何来源中都不存在",
                ref.line, ref.col)
        if len(matches) > 1:
            names = ", ".join(src["binding"] for src, _ in matches)
            raise LineageError(
                f"字段 {ref.name} 存在歧义，同时出现在 {names}，"
                "请使用 表别名.列名 限定", ref.line, ref.col)
        return matches[0]

    # ---- Query / CTE 递归 ----

    def run(self):
        self._process_query(self.query, visible_ctes={})
        new_sql = self._apply_edits()
        return new_sql

    def _process_query(self, query, visible_ctes):
        # 内层可引用外层 CTE；同名时内层遮蔽外层（与 analyzer 一致）
        cte_map = dict(visible_ctes)
        for cte in query.ctes:
            inner_nodes = self._process_query(cte.query, cte_map)
            nodes = self._apply_cte_aliases(cte, inner_nodes)
            key = cte.name.lower()
            if key in cte_map and key not in visible_ctes:
                raise LineageError(f"CTE 名称 {cte.name} 在同一 WITH 中重复定义",
                                   cte.line, cte.col)
            cte_map[key] = nodes

        branch_outputs = []
        for core in query.cores:
            branch_outputs.append(self._process_core(core, cte_map))

        if len(branch_outputs) == 1:
            return branch_outputs[0]
        return self._merge_union(branch_outputs)

    def _apply_cte_aliases(self, cte, nodes):
        if not cte.column_aliases:
            return nodes
        aliases = cte.column_aliases
        if len(aliases) != len(nodes):
            raise LineageError(
                f"CTE {cte.name} 的列别名清单有 {len(aliases)} 列，"
                f"但其查询输出 {len(nodes)} 列", cte.line, cte.col)
        # 显式列别名清单：输出接口名固定，不随源列改名
        return [RNode(alias, n.origin, renamed=False, sensitive=n.sensitive)
                for alias, n in zip(aliases, nodes)]

    def _merge_union(self, branch_outputs):
        width = len(branch_outputs[0])
        for bi, nodes in enumerate(branch_outputs[1:], start=2):
            if len(nodes) != width:
                raise LineageError(
                    f"UNION ALL 各分支列数不一致：第 1 个分支 {width} 列，"
                    f"第 {bi} 个分支 {len(nodes)} 列")
        merged = []
        for pos in range(width):
            peers = [branch[pos] for branch in branch_outputs]
            origins = {p.origin for p in peers}
            target_origin = (self.tkey, self.old_lower)
            # UNION 输出列名由第一分支决定（与 SQLite 行为一致）：
            # 第一分支显式别名保持旧名时，下游接口名不变（renamed=False），
            # 即使该位置仍直通目标源列，下游引用也不得改名。
            first = peers[0]
            if origins == {target_origin}:
                origin = target_origin
            elif len(origins) == 1:
                origin = next(iter(origins))
            else:
                origin = None
            merged.append(RNode(first.name, origin, renamed=first.renamed))
        return merged

    # ---- 单个 SELECT 块 ----

    def _process_core(self, core, cte_map):
        sources = self._bind_sources(core, cte_map)

        # WHERE / JOIN ON 中的引用同样按绑定改写
        if core.where is not None:
            self._rewrite_expr_refs(core.where, sources)
        for join in core.joins:
            self._rewrite_expr_refs(join.on_expr, sources)

        outputs = []
        for rc in core.result_cols:
            if isinstance(rc, ast.Star):
                outputs.extend(self._expand_star(rc, sources, qualified=False))
            elif isinstance(rc, ast.TableStar):
                src = self._find_source(rc.table, sources, rc)
                outputs.extend(self._expand_star(rc, [src], qualified=True))
            else:
                outputs.append(self._process_result_column(rc, sources))
        if not outputs:
            raise LineageError("SELECT 结果列为空", core.line, core.col)
        return outputs

    def _find_source(self, table_name, sources, star_node):
        key = table_name.lower()
        for src in sources:
            if src["binding"] == key:
                return src
        raise LineageError(
            f"{table_name}.* 中的表名/别名在 FROM/JOIN 中不存在",
            star_node.line, star_node.col)

    def _expand_star(self, star_node, sources, qualified):
        """星号展开：仅当展开结果包含被改名列时才就地展开，否则保留 *。"""
        columns = []
        contains_renamed = False
        for src in sources:
            for node in src["nodes"]:
                if node.renamed:
                    contains_renamed = True
                columns.append((src, node))
        if not contains_renamed:
            # 不涉及目标列：星号原样保留，输出列环境仍按展开计算
            return [node for _, node in columns]

        parts = []
        for src, node in columns:
            if qualified:
                parts.append(f"{src['binding']}.{node.name}")
            else:
                # 多来源时补限定名，避免裸列名在展开后产生歧义
                parts.append(f"{src['binding']}.{node.name}"
                             if len(sources) > 1 else node.name)
        # 定位原始 star token（裸 * 或 t.*）并整体替换
        if isinstance(star_node, ast.TableStar):
            # TableStar 占用三个 token：表名 . *；用行列号找到 star 起点
            start, _ = self._span(star_node.line, star_node.col, 1)
            # 找到 star token 的结束位置：从起点扫描到第一个 '*'
            star_idx = self.sql.index("*", start)
            self.edits.append((start, star_idx + 1, ", ".join(parts)))
            self.expanded_stars.append({
                "line": star_node.line, "col": star_node.col,
                "columns": [p for p in parts],
            })
        else:
            start, end = self._span(star_node.line, star_node.col, 1)
            self.edits.append((start, end, ", ".join(parts)))
            self.expanded_stars.append({
                "line": star_node.line, "col": star_node.col,
                "columns": [p for p in parts],
            })
        self.notes.append(
            f"第 {star_node.line} 行星号已展开为 {len(parts)} 个显式列（仅目标列改名）")
        return [node for _, node in columns]

    def _process_result_column(self, rc, sources):
        self._rewrite_expr_refs(rc.expr, sources)
        if rc.alias:
            # 显式别名：下游列接口名不变
            origin = self._single_origin(rc.expr, sources)
            return RNode(rc.alias, origin, renamed=False)
        if isinstance(rc.expr, ast.ColumnRef):
            _, node = self._resolve(rc.expr, sources)
            # 无别名的直通列：输出名跟随来源列名
            return RNode(node.name, node.origin, renamed=node.renamed,
                         sensitive=node.sensitive)
        # 复合表达式：默认列名为规范化表达式文本（其中的列引用已就地改名）
        name = canonical_expr(rc.expr)
        return RNode(name, None, renamed=False)

    def _single_origin(self, expr, sources):
        """若表达式只是单列引用，返回其绑定源列 origin；否则 None。"""
        if isinstance(expr, ast.ColumnRef):
            _, node = self._resolve(expr, sources)
            return node.origin
        return None

    # ---- 表达式内引用改写 ----

    def _rewrite_expr_refs(self, expr, sources):
        for ref in collect_refs(expr):
            src, node = self._resolve(ref, sources)
            # 只有在当前环境中实际改名、且绑定到目标源列的列引用才改写；
            # 显式别名/CTE 遮蔽导致列名保持不变的，即使直通目标源列也不动
            if node.origin != (self.tkey, self.old_lower) or not node.renamed:
                continue
            self.affected = True
            if ref.table is not None:
                # 限定名：仅替换点号后的列名 token
                self._replace_qualified_ref(ref, node)
            else:
                token = self._find_token(ref.line, ref.col, ref.name)
                if node.name != token.value:
                    self._replace_token(token, node.name,
                                        "column", old_text=ref.name)

    def _replace_qualified_ref(self, ref, node):
        # 限定列名 a.b 由两个 word token 与一个 dot 组成，只替换 b；
        # token 值已去引号，故按 ref 中记录的名称直接比较即可
        toks = self._tokens_at_line(ref.line)
        for i, tok in enumerate(toks):
            if (tok.kind == "word" and tok.value == ref.table
                    and tok.col == ref.col):
                col_tok = toks[i + 2]
                if col_tok.kind != "word" or col_tok.value != ref.name:
                    raise LineageError("内部错误：限定列名定位失败，改名已中止")
                if col_tok.value != node.name:
                    self._replace_token(col_tok, node.name,
                                        "column", old_text=ref.name)
                return
        raise LineageError("内部错误：限定列名 token 未找到，改名已中止")

    def _find_token(self, line, col, value):
        for tok in self._tokens_at_line(line):
            if tok.col == col and tok.kind == "word" and tok.value == value:
                return tok
        raise LineageError("内部错误：列引用 token 未找到，改名已中止")

    def _tokens_at_line(self, line):
        if not hasattr(self, "_tokens_by_line"):
            self._tokens_by_line = {}
            for tok in tokenize(self.sql):
                self._tokens_by_line.setdefault(tok.line, []).append(tok)
        return self._tokens_by_line[line]

    # ---- 编辑应用 ----

    def _apply_edits(self):
        if not self.edits:
            return self.sql
        # 同一 token 不允许被改写两次
        spans = sorted(self.edits, key=lambda e: (e[0], e[1]))
        for (s1, e1, _), (s2, e2, _) in zip(spans, spans[1:]):
            if s2 < e1:
                raise LineageError("内部错误：改名区间重叠，已中止")
        pieces = []
        cursor = 0
        for start, end, replacement in spans:
            pieces.append(self.sql[cursor:start])
            pieces.append(replacement)
            cursor = end
        pieces.append(self.sql[cursor:])
        return "".join(pieces)


# ---------------------------------------------------------------- 前后等价校验

def _canonicalize_view_model(model, old_name, new_name):
    """把改名后模型中的新列名还原为旧列名，便于与改名前模型逐字段比较。"""
    pat = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(new_name)
                     + r"(?![A-Za-z0-9_$])")

    def fix_text(text):
        return pat.sub(old_name, text or "")

    def fix_deps(deps):
        out = []
        for dep in deps:
            paths = []
            for path in dep["paths"]:
                paths.append([
                    {k: (fix_text(v) if isinstance(v, str) else v)
                     for k, v in hop.items()}
                    for hop in path
                ])
            out.append({**dep, "column": fix_text(dep["column"]), "paths": paths})
        return out

    out = {"outputs": [], "ctes": [], "base_tables": []}
    for col in model["outputs"]:
        out["outputs"].append({
            "position": col["position"],
            "name": fix_text(col["name"]),
            "expr_text": fix_text(col["expr_text"]),
            "from_star": col["from_star"],
            "sensitive": col["sensitive"],
            "sensitive_kind": col["sensitive_kind"],
            "value_sources": fix_deps(col["value_sources"]),
            "condition_sources": fix_deps(col["condition_sources"]),
        })
    for cte in model["ctes"]:
        out["ctes"].append({
            "name": cte["name"],
            "label": cte["label"],
            "columns": [
                {"name": fix_text(c["name"]),
                 "sensitive": c["sensitive"],
                 "sensitive_kind": c["sensitive_kind"],
                 "expr_text": fix_text(c["expr_text"])}
                for c in cte["columns"]
            ],
        })
    for tbl in model["base_tables"]:
        out["base_tables"].append({
            "name": tbl["name"],
            "columns": [
                {"name": fix_text(c["name"]),
                 "sensitive": c["sensitive"],
                 "position": c["position"]}
                for c in tbl["columns"]
            ],
        })
    return out


def _norm_deps(deps):
    """把溯源依赖归一化为与分析器实例无关的可比较结构（去掉自增 base_id）。

    星号展开为显式列后，hop 会从 {'kind':'star'} 变为
    {'kind':'value', 'expr':...}：这属于允许的显式化，故比较时忽略
    hop 的 kind 与 expr 键，只保留 block/name/clause/ref/branch 等结构。
    """
    _IGNORE_HOP_KEYS = {"kind", "expr"}
    out = []
    for dep in deps:
        out.append((
            dep["table"],
            dep["column"],
            dep["sensitive"],
            tuple(tuple(tuple(sorted((k, v) for k, v in hop.items()
                                     if k not in _IGNORE_HOP_KEYS))
                        for hop in path)
                  for path in dep["paths"]),
        ))
    return sorted(out)


def _assert_same_model(before, after_canon, view_name):
    def fail(detail):
        raise LineageError(f"视图 {view_name} 改名前后溯源结果不一致，整批拒绝：{detail}")

    if len(before["outputs"]) != len(after_canon["outputs"]):
        fail(f"输出列数 {len(before['outputs'])} -> "
             f"{len(after_canon['outputs'])}")
    for i, (a, b) in enumerate(zip(before["outputs"], after_canon["outputs"]),
                                start=1):
        if a["position"] != b["position"]:
            fail(f"第 {i} 列位置变化")
        if a["name"] != b["name"]:
            fail(f"第 {i} 列名 {a['name']!r} -> {b['name']!r}，下游接口不允许变化")
        # 星号展开允许补限定名（cust_id -> c.cust_id），比较时做双向归一化
        if (a["expr_text"] or "").split(".")[-1] != \
                (b["expr_text"] or "").split(".")[-1]:
            fail(f"第 {i} 列表达式 {a['expr_text']!r} -> {b['expr_text']!r}")
        # 星号展开为显式列后 from_star 由 true 变 false，属于允许的显式化，
        # 不允许反向（显式列变星号）或其他变化
        if a["from_star"] != b["from_star"] and not (
                a["from_star"] is True and b["from_star"] is False):
            fail(f"第 {i} 列星号标记异常变化 {a['from_star']} -> {b['from_star']}")
        if a["sensitive"] != b["sensitive"] or \
                sorted(a["sensitive_kind"]) != sorted(b["sensitive_kind"]):
            fail(f"第 {i} 列敏感标记变化")
        if _norm_deps(a["value_sources"]) != _norm_deps(b["value_sources"]):
            fail(f"第 {i} 列值来源变化")
        if _norm_deps(a["condition_sources"]) != _norm_deps(b["condition_sources"]):
            fail(f"第 {i} 列条件来源变化")
    if len(before["ctes"]) != len(after_canon["ctes"]):
        fail("CTE 数量变化")
    for c1, c2 in zip(before["ctes"], after_canon["ctes"]):
        if c1["name"] != c2["name"]:
            fail(f"CTE 名称 {c1['name']!r} -> {c2['name']!r}")
        if [c["name"] for c in c1["columns"]] != \
                [c["name"] for c in c2["columns"]]:
            fail(f"CTE {c1['name']} 输出列名/顺序变化")
        if [(c["sensitive"], c["sensitive_kind"]) for c in c1["columns"]] != \
                [(c["sensitive"], c["sensitive_kind"]) for c in c2["columns"]]:
            fail(f"CTE {c1['name']} 敏感标记变化")


def _analyze(schema_text, sql):
    tables = parse_schemas(schema_text)
    query = parse_sql(sql)
    return Analyzer(tables).analyze(query)


# ---------------------------------------------------------------- 批量主入口

def plan_rename(schema_text, views, table_name, old_name, new_name):
    """对整批视图做改名预览。

    views: [{"name": str, "sql": str}, ...]
    返回: {"schema_text": 新源表文本, "views": [RenamePlan, ...]}
    任何冲突或任一视图失败均抛 LineageError，不产生部分结果。
    """
    # 1. 前置冲突校验（同时验证旧结构可解析）
    tables, spec, old_col = check_conflicts(
        schema_text, table_name, old_name, new_name)
    old_schema_text = schema_text
    new_schema_text = rename_schema_text(
        old_schema_text, table_name, old_name, new_name)

    # 2. 先对全部视图做一次改名前溯源：任一视图本身不合法即整批拒绝
    before_models = {}
    for view in views:
        try:
            before_models[view["name"]] = _analyze(old_schema_text, view["sql"])
        except LineageError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性兜底
            raise LineageError(
                f"视图 {view['name']} 改名前分析失败：{type(exc).__name__}: {exc}")

    plans = []
    # 3. 逐视图改写（全部成功才继续）
    for view in views:
        query = parse_sql(view["sql"])
        renamer = _ViewRenamer(query, view["sql"], tables,
                               spec.name, old_col.name, new_name)
        new_sql = renamer.run()
        plans.append(RenamePlan(
            view_name=view["name"],
            old_sql=view["sql"],
            new_sql=new_sql,
            changed_refs=renamer.changed_refs,
            expanded_stars=renamer.expanded_stars,
            affected=renamer.affected or bool(renamer.expanded_stars),
            notes=renamer.notes,
        ))

    # 4. 新结构 + 新 SQL 全部重新溯源，并与改名前做等价校验
    for plan in plans:
        try:
            after_model = _analyze(new_schema_text, plan.new_sql)
        except LineageError:
            raise
        except Exception as exc:  # pragma: no cover
            raise LineageError(
                f"视图 {plan.view_name} 改名后分析失败：{type(exc).__name__}: {exc}")
        canon = _canonicalize_view_model(after_model, old_col.name, new_name)
        _assert_same_model(before_models[plan.view_name], canon, plan.view_name)

        # 5. 防误伤：字符串字面量中的旧名必须原样保留
        for tok in tokenize(plan.new_sql):
            if tok.kind == "string" and old_name in tok.value:
                # 与原 SQL 中同位置字符串比较
                old_strings = [t.value for t in tokenize(plan.old_sql)
                               if t.kind == "string"]
                new_strings = [t.value for t in tokenize(plan.new_sql)
                               if t.kind == "string"]
                if old_strings != new_strings:
                    raise LineageError(
                        f"视图 {plan.view_name} 改写误伤了字符串字面量，整批拒绝")

    return {"schema_text": new_schema_text, "plans": plans}


def plan_to_dict(plan):
    return {
        "view_name": plan.view_name,
        "old_sql": plan.old_sql,
        "new_sql": plan.new_sql,
        "changed_refs": plan.changed_refs,
        "expanded_stars": plan.expanded_stars,
        "affected": plan.affected,
        "notes": plan.notes,
    }
