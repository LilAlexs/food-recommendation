from rag_modules.query_normalization import clean_query, has_topic_switch, DURATION_SUFFIX
"""Local, authoritative recipe identity gate. Similarity never grants identity."""
import difflib
import hashlib
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class CatalogUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    name: str
    path: Path
    source: str
    digest: str


def normalize(name):
    return re.sub(r"\s+", "", name).strip("《》\"'，。？！?！")


def title(text):
    match = re.search(r"^#\s+(.+)$", text, re.M)
    if not match:
        raise CatalogUnavailable("Recipe lacks a top-level title")
    return normalize(re.sub(r"的做法$", "", match.group(1)))


def requested_name(query):
    """Bounded forms, never substring-match an unknown dish to a known dish."""
    original = query.strip().rstrip("？?。！!")
    text = clean_query(query).rstrip("？?。！!")
    text = re.sub(r"^(?:请问|请告诉我|请教我|我想知道|我想学做|我想做)", "", text).strip()
    if text.startswith("查看菜谱：") or text.startswith("查看菜谱:"):
        return normalize(text[5:])
    patterns = [r"(?:怎么做|如何做|如何制作|教我做)(.+)",
        r"(.+?)(?:怎么做(?:呢)?|如何做|的做法|做法|需要哪些材料|需要什么材料|用什么食材|的食材|的材料)",
        r"(.+?)" + DURATION_SUFFIX]
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            name = normalize(match.group(1))
            return name if name else None
    if text != original and re.fullmatch(r"[\u4e00-\u9fff]{2,24}",text) and not re.search(
            r"推荐|吃什么|有哪些|有什么|为什么|区别|搭配|比较|关系", text):
        return normalize(text)
    return None


class RecipeCatalog:
    def __init__(self, root, aliases=None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise CatalogUnavailable("Recipe directory unavailable")
        self.by_name = {}
        self.groups = {}
        self.invalid_names = set()
        try:
            for path in sorted(self.root.rglob("*.md")):
                if path.stem in {"README", "示例菜"}:
                    continue
                if not path.resolve().is_relative_to(self.root):
                    continue
                content = path.read_text(encoding="utf-8-sig")
                # Non-recipe notes are not indexed as dishes.
                if not re.search(r"^##\s+(?:操作|制作|做法|步骤)",content,re.M):
                    continue
                try:
                    name = title(content)
                except CatalogUnavailable:
                    self.invalid_names.add(normalize(path.stem))
                    continue
                # A disagreement is a data error, never silently trust filename metadata.
                if name != normalize(re.sub(r"的做法$", "", path.stem)):
                    self.invalid_names.update((name, normalize(path.stem)))
                    continue
                relative = path.relative_to(self.root).as_posix()
                recipe = Recipe("local:"+hashlib.sha256(relative.encode()).hexdigest()[:20],
                    name,path,relative,hashlib.sha256(content.encode()).hexdigest())
                self.by_name.setdefault(name, []).append(recipe)
                for parent in path.relative_to(self.root).parents:
                    if re.fullmatch(r"[\u4e00-\u9fff]{2,20}",parent.name):
                        self.groups.setdefault(normalize(parent.name),set()).add(name)
        except (OSError, UnicodeError) as exc:
            raise CatalogUnavailable("Cannot read recipe catalog") from exc
        if not self.by_name:
            raise CatalogUnavailable("No valid recipes available")
        config = aliases or {}
        self.aliases=config.get("aliases",{})
        self.choices=config.get("choices",{})
        for alias,canonical in self.aliases.items():
            if canonical not in self.by_name:
                raise CatalogUnavailable("Alias target missing")
            if alias in self.by_name and alias != canonical:
                raise CatalogUnavailable("Alias conflicts with real recipe")

    def resolve(self, name):
        name = normalize(name)
        canonical = self.aliases.get(name,name)
        if name in self.invalid_names or canonical in self.invalid_names:
            raise CatalogUnavailable("Requested recipe has inconsistent title/source")
        if canonical in self.by_name:
            matches = self.by_name[canonical]
            if len(matches)==1:
                return dict(status="alias" if canonical!=name else "exact",requested=name,
                            recipe=matches[0],suggestions=[])
            return dict(status="needs_confirmation",requested=name,recipe=None,
                        suggestions=[],message="存在多份同名菜谱，请补充具体版本；当前不会自动选择。")
        choices = set(self.choices.get(name,[])) | self.groups.get(name,set())
        choices = sorted(n for n in choices if n in self.by_name)
        if choices:
            return dict(status="needs_confirmation",requested=name,recipe=None,suggestions=choices[:5])
        # Multi-dish requests are clarified rather than merged into one recipe.
        if any(sep in name for sep in ("和","与","、","以及")):
            return dict(status="needs_confirmation",requested=name,recipe=None,suggestions=[],
                        message="请一次指定一道菜，避免混合不同菜谱的步骤。")
        # These are proposals ONLY, never used as generation context.
        candidates=difflib.get_close_matches(name,sorted(self.by_name),n=3,cutoff=.55)
        return dict(status="not_found",requested=name,recipe=None,suggestions=candidates)

    def document(self, recipe):
        try:
            content=recipe.path.read_text(encoding="utf-8-sig")
        except (OSError,UnicodeError) as exc:
            raise CatalogUnavailable("Recipe source cannot be read") from exc
        if hashlib.sha256(content.encode()).hexdigest()!=recipe.digest or title(content)!=recipe.name:
            raise CatalogUnavailable("Recipe source changed; reload catalog")
        from langchain_core.documents import Document
        return Document(page_content=content,metadata=dict(recipe_id=recipe.recipe_id,
            recipe_name=recipe.name,source=recipe.source,source_type="local_recipe_markdown",
            content_sha256=recipe.digest))


@lru_cache(maxsize=2)
def get_catalog(root=None):
    directory = Path(root) if root else Path(__file__).resolve().parents[1]/"data"/"dishes"
    try:
        config=json.loads(Path(__file__).with_name("recipe_aliases.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogUnavailable("Alias configuration unavailable") from exc
    return RecipeCatalog(directory,config)


def resolve_request(query, enhanced_query=None, confirmed_recipe=None, has_identity_state=False):
    raw=requested_name(query)
    if raw is None and re.fullmatch(r"[\u4e00-\u9fff]{2,24}", query.strip()) and not re.search(
            r"推荐|吃什么|有哪些|有什么|为什么|区别|搭配|比较|关系|可以|谢谢|你好|好的|取消|不要|不放|这道|那道|它", query):
        raw=normalize(query)
    pronouns={"它","这道菜","那道菜","这个","那个"}
    if raw in pronouns and has_topic_switch(query):
        return dict(status="needs_confirmation",requested=raw,recipe=None,suggestions=[],
                    message="你已切换话题，请明确新问题指的是哪道菜。")
    if raw in pronouns and has_identity_state:
        if isinstance(confirmed_recipe, dict) and confirmed_recipe.get("name"):
            raw=confirmed_recipe["name"]
        else:
            return dict(status="needs_confirmation",requested=raw,recipe=None,suggestions=[],
                        message="当前没有已确认的菜谱，请先明确菜名。")
    if raw in pronouns and enhanced_query:
        resolved=requested_name(enhanced_query)
        if resolved and resolved not in pronouns and "\n" not in resolved:
            raw=resolved
    if raw is None:
        return None
    if raw in pronouns:
        return dict(status="needs_confirmation",requested=raw,recipe=None,suggestions=[],
                    message="请明确是哪道菜，避免把上一轮的多道推荐混在一起。")
    # Preserve a constraint-bearing original question in generation, but identify a
    # dish only when a single known suffix follows an explicit modifier.
    catalog=get_catalog(os.getenv("RAG_RECIPE_DIR") or None)
    result=catalog.resolve(raw)
    if result["status"]=="not_found" and not re.search(r"和|与|、|以及", raw) and re.search(r"不放|不含|不要|过敏|分钟",raw):
        matches=[name for name in list(catalog.by_name)+list(catalog.aliases) if raw.endswith(name)]
        if matches:
            target=max(matches,key=len)
            result=catalog.resolve(target)
    return result


def public_result(result):
    status=result["status"]
    recipe=result.get("recipe")
    if recipe:
        return dict(recipe_status=status,answer_source="recipe_library",
                    recipe_id=recipe.recipe_id,recipe_name=recipe.name,source=recipe.source)
    names=result.get("suggestions",[])
    options=[dict(label="查看"+name,message="查看菜谱："+name) for name in names]
    if status=="not_found":
        text="菜谱库暂未收录或未识别到“"+result["requested"]+"”，不会自动用其他菜的做法替代。"
    else:
        text=result.get("message") or "“"+result["requested"]+"”需要确认具体版本，当前不会自动选菜。"
    if names:
        text+="\n可选的库内菜谱（需要你确认）：\n"+"\n".join(
            str(i+1)+". "+name+"（回复：查看菜谱："+name+"）" for i,name in enumerate(names))
    text+="\n你也可以换一个明确菜名，或核对菜名的写法。"
    return dict(recipe_status=status,answer_source="catalog_notice",response=text,options=options)
