"""
Webnovel Dashboard - FastAPI 主应用

仅提供 GET 接口（严格只读）；所有文件读取经过 path_guard 防穿越校验。
"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .path_guard import safe_resolve
from .watcher import FileWatcher

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
_project_root: Path | None = None
_workspace_root: Path | None = None
_watcher = FileWatcher()

STATIC_DIR = Path(__file__).parent / "frontend" / "dist"

# 全局风格配置缓存
_writing_styles_cache: dict | None = None


def _load_writing_styles() -> dict:
    """加载全局风格配置"""
    global _writing_styles_cache
    if _writing_styles_cache is not None:
        return _writing_styles_cache

    config_path = Path(__file__).parent.parent / "config" / "writing-styles.json"
    if config_path.exists():
        _writing_styles_cache = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        _writing_styles_cache = {"styles": [], "scene_types": [], "genre_style_map": {}}
    return _writing_styles_cache


def _get_default_style_config(genre: str) -> dict:
    """根据题材获取默认风格配置"""
    styles = _load_writing_styles()
    genre_map = styles.get("genre_style_map", {})
    scene_types = styles.get("scene_types", [])

    primary = genre_map.get(genre, "standard")

    # 构建场景适配器
    adapters = {}
    for scene in scene_types:
        adapters[scene["id"]] = {
            "style": scene.get("default_style", primary),
            "blend": scene.get("default_blend", 0.8)
        }

    return {
        "version": "1.0",
        "primary": primary,
        "intelligence": {
            "enabled": False,  # 旧项目默认关闭
            "scene_adapters": adapters
        }
    }


def _get_project_root() -> Path:
    if _project_root is None:
        raise HTTPException(status_code=500, detail="项目根目录未配置")
    return _project_root


def _webnovel_dir() -> Path:
    return _get_project_root() / ".webnovel"


def _get_workspace_root() -> Path:
    if _workspace_root is not None:
        return _workspace_root
    # 默认使用项目根目录的父目录作为工作区
    return _get_project_root().parent


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(project_root: str | Path | None = None, workspace_root: str | Path | None = None) -> FastAPI:
    global _project_root, _workspace_root

    if project_root:
        _project_root = Path(project_root).resolve()

    if workspace_root:
        _workspace_root = Path(workspace_root).resolve()
    elif _project_root:
        # 默认使用项目父目录作为工作区
        _workspace_root = _project_root.parent

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        webnovel = _webnovel_dir()
        if webnovel.is_dir():
            _watcher.start(webnovel, asyncio.get_running_loop())
        try:
            yield
        finally:
            _watcher.stop()

    app = FastAPI(title="Webnovel Dashboard", version="0.1.0", lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    # ===========================================================
    # API：项目元信息
    # ===========================================================

    @app.get("/api/project/info")
    def project_info():
        """返回 state.json 完整内容（只读）。"""
        state_path = _webnovel_dir() / "state.json"
        if not state_path.is_file():
            raise HTTPException(404, "state.json 不存在")
        return json.loads(state_path.read_text(encoding="utf-8"))

    # ===========================================================
    # API：实体数据库（index.db 只读查询）
    # ===========================================================

    def _get_db() -> sqlite3.Connection:
        db_path = _webnovel_dir() / "index.db"
        if not db_path.is_file():
            raise HTTPException(404, "index.db 不存在")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _fetchall_safe(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
        """执行只读查询；若目标表不存在（旧库），返回空列表。"""
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise HTTPException(status_code=500, detail=f"数据库查询失败: {exc}") from exc

    @app.get("/api/entities")
    def list_entities(
        entity_type: Optional[str] = Query(None, alias="type"),
        include_archived: bool = False,
    ):
        """列出所有实体（可按类型过滤）。"""
        with closing(_get_db()) as conn:
            q = "SELECT * FROM entities"
            params: list = []
            clauses: list[str] = []
            if entity_type:
                clauses.append("type = ?")
                params.append(entity_type)
            if not include_archived:
                clauses.append("is_archived = 0")
            if clauses:
                q += " WHERE " + " AND ".join(clauses)
            q += " ORDER BY last_appearance DESC"
            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/entities/{entity_id}")
    def get_entity(entity_id: str):
        with closing(_get_db()) as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if not row:
                raise HTTPException(404, "实体不存在")
            return dict(row)

    @app.get("/api/relationships")
    def list_relationships(entity: Optional[str] = None, limit: int = 200):
        with closing(_get_db()) as conn:
            if entity:
                rows = conn.execute(
                    "SELECT * FROM relationships WHERE from_entity = ? OR to_entity = ? ORDER BY chapter DESC LIMIT ?",
                    (entity, entity, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM relationships ORDER BY chapter DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/relationship-events")
    def list_relationship_events(
        entity: Optional[str] = None,
        from_chapter: Optional[int] = None,
        to_chapter: Optional[int] = None,
        limit: int = 200,
    ):
        with closing(_get_db()) as conn:
            q = "SELECT * FROM relationship_events"
            params: list = []
            clauses: list[str] = []
            if entity:
                clauses.append("(from_entity = ? OR to_entity = ?)")
                params.extend([entity, entity])
            if from_chapter is not None:
                clauses.append("chapter >= ?")
                params.append(from_chapter)
            if to_chapter is not None:
                clauses.append("chapter <= ?")
                params.append(to_chapter)
            if clauses:
                q += " WHERE " + " AND ".join(clauses)
            q += " ORDER BY chapter DESC, id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/chapters")
    def list_chapters():
        with closing(_get_db()) as conn:
            rows = conn.execute("SELECT * FROM chapters ORDER BY chapter ASC").fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/scenes")
    def list_scenes(chapter: Optional[int] = None, limit: int = 500):
        with closing(_get_db()) as conn:
            if chapter is not None:
                rows = conn.execute(
                    "SELECT * FROM scenes WHERE chapter = ? ORDER BY scene_index ASC", (chapter,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scenes ORDER BY chapter ASC, scene_index ASC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/reading-power")
    def list_reading_power(limit: int = 50):
        with closing(_get_db()) as conn:
            rows = conn.execute(
                "SELECT * FROM chapter_reading_power ORDER BY chapter DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/review-metrics")
    def list_review_metrics(limit: int = 20):
        with closing(_get_db()) as conn:
            rows = conn.execute(
                "SELECT * FROM review_metrics ORDER BY end_chapter DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/state-changes")
    def list_state_changes(entity: Optional[str] = None, limit: int = 100):
        with closing(_get_db()) as conn:
            if entity:
                rows = conn.execute(
                    "SELECT * FROM state_changes WHERE entity_id = ? ORDER BY chapter DESC LIMIT ?",
                    (entity, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM state_changes ORDER BY chapter DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/aliases")
    def list_aliases(entity: Optional[str] = None):
        with closing(_get_db()) as conn:
            if entity:
                rows = conn.execute(
                    "SELECT * FROM aliases WHERE entity_id = ?", (entity,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM aliases").fetchall()
            return [dict(r) for r in rows]

    # ===========================================================
    # API：扩展表（v5.3+ / v5.4+）
    # ===========================================================

    @app.get("/api/overrides")
    def list_overrides(status: Optional[str] = None, limit: int = 100):
        with closing(_get_db()) as conn:
            if status:
                return _fetchall_safe(
                    conn,
                    "SELECT * FROM override_contracts WHERE status = ? ORDER BY chapter DESC LIMIT ?",
                    (status, limit),
                )
            return _fetchall_safe(
                conn,
                "SELECT * FROM override_contracts ORDER BY chapter DESC LIMIT ?",
                (limit,),
            )

    @app.get("/api/debts")
    def list_debts(status: Optional[str] = None, limit: int = 100):
        with closing(_get_db()) as conn:
            if status:
                return _fetchall_safe(
                    conn,
                    "SELECT * FROM chase_debt WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                )
            return _fetchall_safe(
                conn,
                "SELECT * FROM chase_debt ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )

    @app.get("/api/debt-events")
    def list_debt_events(debt_id: Optional[int] = None, limit: int = 200):
        with closing(_get_db()) as conn:
            if debt_id is not None:
                return _fetchall_safe(
                    conn,
                    "SELECT * FROM debt_events WHERE debt_id = ? ORDER BY chapter DESC, id DESC LIMIT ?",
                    (debt_id, limit),
                )
            return _fetchall_safe(
                conn,
                "SELECT * FROM debt_events ORDER BY chapter DESC, id DESC LIMIT ?",
                (limit,),
            )

    @app.get("/api/invalid-facts")
    def list_invalid_facts(status: Optional[str] = None, limit: int = 100):
        with closing(_get_db()) as conn:
            if status:
                return _fetchall_safe(
                    conn,
                    "SELECT * FROM invalid_facts WHERE status = ? ORDER BY marked_at DESC LIMIT ?",
                    (status, limit),
                )
            return _fetchall_safe(
                conn,
                "SELECT * FROM invalid_facts ORDER BY marked_at DESC LIMIT ?",
                (limit,),
            )

    @app.get("/api/rag-queries")
    def list_rag_queries(query_type: Optional[str] = None, limit: int = 100):
        with closing(_get_db()) as conn:
            if query_type:
                return _fetchall_safe(
                    conn,
                    "SELECT * FROM rag_query_log WHERE query_type = ? ORDER BY created_at DESC LIMIT ?",
                    (query_type, limit),
                )
            return _fetchall_safe(
                conn,
                "SELECT * FROM rag_query_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

    @app.get("/api/tool-stats")
    def list_tool_stats(tool_name: Optional[str] = None, limit: int = 200):
        with closing(_get_db()) as conn:
            if tool_name:
                return _fetchall_safe(
                    conn,
                    "SELECT * FROM tool_call_stats WHERE tool_name = ? ORDER BY created_at DESC LIMIT ?",
                    (tool_name, limit),
                )
            return _fetchall_safe(
                conn,
                "SELECT * FROM tool_call_stats ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

    @app.get("/api/checklist-scores")
    def list_checklist_scores(limit: int = 100):
        with closing(_get_db()) as conn:
            return _fetchall_safe(
                conn,
                "SELECT * FROM writing_checklist_scores ORDER BY chapter DESC LIMIT ?",
                (limit,),
            )

    # ===========================================================
    # API：文档浏览（正文/大纲/设定集 —— 只读）
    # ===========================================================

    @app.get("/api/files/tree")
    def file_tree():
        """列出 正文/、大纲/、设定集/ 三个目录的树结构。"""
        root = _get_project_root()
        result = {}
        for folder_name in ("正文", "大纲", "设定集"):
            folder = root / folder_name
            if not folder.is_dir():
                result[folder_name] = []
                continue
            result[folder_name] = _walk_tree(folder, root)
        return result

    @app.get("/api/files/read")
    def file_read(path: str):
        """只读读取一个文件内容（限 正文/大纲/设定集 目录）。"""
        root = _get_project_root()
        resolved = safe_resolve(root, path)

        # 二次限制：只允许三大目录
        allowed_parents = [root / n for n in ("正文", "大纲", "设定集")]
        if not any(_is_child(resolved, p) for p in allowed_parents):
            raise HTTPException(403, "仅允许读取 正文/大纲/设定集 目录下的文件")

        if not resolved.is_file():
            raise HTTPException(404, "文件不存在")

        # 文本文件直接读；其他情况返回占位信息
        try:
            content = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = "[二进制文件，无法预览]"

        return {"path": path, "content": content}

    # ===========================================================
    # API：项目管理（多项目切换）
    # ===========================================================

    class CreateProjectRequest(BaseModel):
        title: str
        genre: str
        project_dir: Optional[str] = None
        protagonist_name: Optional[str] = ""
        target_chapters: Optional[int] = 600
        golden_finger_name: Optional[str] = ""
        golden_finger_type: Optional[str] = ""
        core_selling_points: Optional[str] = ""
        # 新增：风格配置
        writing_style: Optional[dict] = None
        primary_style: Optional[str] = "standard"
        intelligence_enabled: Optional[bool] = True
        scene_adapters: Optional[dict] = None

    @app.get("/api/projects/list")
    def list_projects():
        """列出工作区中的所有项目。"""
        workspace = _get_workspace_root()
        projects = []

        # 搜索工作区下的所有 .webnovel 目录
        for item in workspace.iterdir():
            if item.is_dir() and not item.name.startswith('.') and not item.name.startswith('_'):
                webnovel_dir = item / ".webnovel"
                state_file = webnovel_dir / "state.json"
                if state_file.is_file():
                    try:
                        state = json.loads(state_file.read_text(encoding="utf-8"))
                        project_info = state.get("project_info", {})
                        progress = state.get("progress", {})

                        projects.append({
                            "id": item.name,
                            "path": str(item.resolve()),
                            "title": project_info.get("title", item.name),
                            "genre": project_info.get("genre", "未知"),
                            "current_chapter": progress.get("current_chapter", 0),
                            "total_words": progress.get("total_words", 0),
                            "target_chapters": project_info.get("target_chapters", 0),
                            "is_active": str(item.resolve()) == str(_get_project_root().resolve()),
                        })
                    except Exception:
                        # 跳过无法解析的项目
                        pass

        return {"projects": sorted(projects, key=lambda x: x["title"]), "workspace": str(workspace)}

    @app.get("/api/projects/current")
    def get_current_project():
        """获取当前项目信息。"""
        root = _get_project_root()
        state_file = root / ".webnovel" / "state.json"

        if not state_file.is_file():
            raise HTTPException(404, "当前项目状态文件不存在")

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return {
                "id": root.name,
                "path": str(root),
                "title": state.get("project_info", {}).get("title", root.name),
                "genre": state.get("project_info", {}).get("genre", "未知"),
                "is_active": True,
            }
        except Exception as e:
            raise HTTPException(500, f"读取项目信息失败: {e}")

    class SwitchProjectRequest(BaseModel):
        path: str

    @app.post("/api/projects/switch")
    async def switch_project(request: SwitchProjectRequest):
        """切换当前项目（通过更新 .claude 指针文件）。"""
        global _project_root, _watcher

        project_path = request.path
        if not project_path:
            raise HTTPException(400, "缺少项目路径")

        new_root = Path(project_path).resolve()

        # 验证新项目是有效的 webnovel 项目
        if not (new_root / ".webnovel" / "state.json").is_file():
            raise HTTPException(400, "目标目录不是有效的 webnovel 项目")

        # 更新指针文件
        try:
            cwd = Path.cwd()
            pointer = cwd / ".claude" / ".webnovel-current-project"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(str(new_root), encoding="utf-8")
        except Exception as e:
            raise HTTPException(500, f"更新项目指针失败: {e}")

        # 更新全局状态
        _project_root = new_root

        # 重启文件监控
        webnovel = _webnovel_dir()
        _watcher.stop()
        if webnovel.is_dir():
            _watcher.start(webnovel, asyncio.get_running_loop())

        return {"success": True, "message": f"已切换到项目: {new_root.name}", "path": str(new_root)}

    @app.post("/api/projects/create")
    def create_project(request: CreateProjectRequest):
        """创建新项目。"""
        workspace = _get_workspace_root()

        # 确定项目目录
        if request.project_dir:
            project_dir = workspace / request.project_dir
        else:
            # 使用书名作为目录名（简化处理）
            safe_name = request.title.replace(" ", "_").replace("/", "_")
            project_dir = workspace / safe_name

        # 检查目录是否已存在
        if project_dir.exists():
            raise HTTPException(409, f"目录已存在: {project_dir.name}")

        # 调用 init_project 逻辑
        try:
            # 导入 init_project 模块
            import sys
            scripts_path = Path(__file__).parent.parent / "scripts"
            if str(scripts_path) not in sys.path:
                sys.path.insert(0, str(scripts_path))

            from init_project import init_project

            init_project(
                project_dir=str(project_dir),
                title=request.title,
                genre=request.genre,
                protagonist_name=request.protagonist_name or "",
                target_chapters=request.target_chapters or 600,
                golden_finger_name=request.golden_finger_name or "",
                golden_finger_type=request.golden_finger_type or "",
                core_selling_points=request.core_selling_points or "",
            )

            # 写入风格配置
            try:
                state_path = project_dir / ".webnovel" / "state.json"
                if state_path.exists():
                    state = json.loads(state_path.read_text(encoding="utf-8"))

                    # 使用请求中的风格配置，或根据题材自动生成
                    if request.writing_style:
                        state["project_info"]["writing_style"] = request.writing_style
                    else:
                        # 根据primary_style和intelligence设置构建配置
                        primary = request.primary_style or "standard"
                        adapters = request.scene_adapters or {}

                        # 如果没有提供适配器，使用默认
                        if not adapters:
                            styles = _load_writing_styles()
                            for scene in styles.get("scene_types", []):
                                adapters[scene["id"]] = {
                                    "style": scene.get("default_style", primary),
                                    "blend": scene.get("default_blend", 0.8)
                                }

                        state["project_info"]["writing_style"] = {
                            "version": "1.0",
                            "primary": primary,
                            "intelligence": {
                                "enabled": request.intelligence_enabled if request.intelligence_enabled is not None else True,
                                "scene_adapters": adapters
                            }
                        }

                    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                # 风格配置写入失败不影响项目创建
                print(f"Warning: 写入风格配置失败: {e}")

            return {
                "success": True,
                "message": f"项目创建成功: {request.title}",
                "path": str(project_dir),
                "project_id": project_dir.name,
            }
        except Exception as e:
            # 清理已创建的目录
            if project_dir.exists():
                import shutil
                shutil.rmtree(project_dir, ignore_errors=True)
            raise HTTPException(500, f"创建项目失败: {e}")

    # ===========================================================
    # API：文档编辑（保存文件）
    # ===========================================================

    class SaveFileRequest(BaseModel):
        path: str
        content: str

    @app.post("/api/files/save")
    @app.put("/api/files/save")
    def file_save(request: SaveFileRequest):
        """保存文件内容（限 正文/大纲/设定集 目录）。"""
        root = _get_project_root()
        resolved = safe_resolve(root, request.path)

        # 二次限制：只允许三大目录
        allowed_parents = [root / n for n in ("正文", "大纲", "设定集")]
        if not any(_is_child(resolved, p) for p in allowed_parents):
            raise HTTPException(403, "仅允许编辑 正文/大纲/设定集 目录下的文件")

        # 确保父目录存在
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # 写入文件
        try:
            resolved.write_text(request.content, encoding="utf-8")
            return {"success": True, "path": request.path, "message": "文件保存成功"}
        except Exception as e:
            raise HTTPException(500, f"保存文件失败: {e}")

    @app.post("/api/files/create")
    def file_create(request: SaveFileRequest):
        """创建新文件（限 正文/大纲/设定集 目录）。"""
        root = _get_project_root()
        resolved = safe_resolve(root, request.path)

        # 二次限制：只允许三大目录
        allowed_parents = [root / n for n in ("正文", "大纲", "设定集")]
        if not any(_is_child(resolved, p) for p in allowed_parents):
            raise HTTPException(403, "仅允许在 正文/大纲/设定集 目录下创建文件")

        # 如果文件已存在，返回错误
        if resolved.exists():
            raise HTTPException(409, "文件已存在")

        # 确保父目录存在
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # 写入文件
        try:
            resolved.write_text(request.content, encoding="utf-8")
            return {"success": True, "path": request.path, "message": "文件创建成功"}
        except Exception as e:
            raise HTTPException(500, f"创建文件失败: {e}")

    # ===========================================================
    # API：风格配置管理
    # ===========================================================

    @app.get("/api/styles/available")
    def get_available_styles():
        """获取可用的写作风格列表"""
        return _load_writing_styles()

    @app.get("/api/styles/config")
    def get_style_config():
        """获取当前项目的风格配置"""
        state_path = _webnovel_dir() / "state.json"
        if not state_path.is_file():
            raise HTTPException(404, "项目状态文件不存在")

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            style_config = state.get("project_info", {}).get("writing_style")

            # 如果没有配置，返回 null（旧项目）
            return {
                "has_config": style_config is not None,
                "config": style_config,
                "available_styles": _load_writing_styles()
            }
        except Exception as e:
            raise HTTPException(500, f"读取风格配置失败: {e}")

    class StyleConfigRequest(BaseModel):
        primary: str
        intelligence_enabled: bool
        scene_adapters: dict

    @app.post("/api/styles/config")
    def update_style_config(request: StyleConfigRequest):
        """更新当前项目的风格配置"""
        state_path = _webnovel_dir() / "state.json"
        if not state_path.is_file():
            raise HTTPException(404, "项目状态文件不存在")

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))

            # 构建风格配置
            style_config = {
                "version": "1.0",
                "primary": request.primary,
                "intelligence": {
                    "enabled": request.intelligence_enabled,
                    "scene_adapters": request.scene_adapters
                }
            }

            state["project_info"]["writing_style"] = style_config
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

            return {"success": True, "config": style_config}
        except Exception as e:
            raise HTTPException(500, f"更新风格配置失败: {e}")

    class InitializeStyleRequest(BaseModel):
        primary: Optional[str] = "standard"
        intelligence_enabled: Optional[bool] = True

    @app.post("/api/styles/initialize")
    def initialize_style(request: InitializeStyleRequest):
        """为旧项目初始化风格配置"""
        state_path = _webnovel_dir() / "state.json"
        if not state_path.is_file():
            raise HTTPException(404, "项目状态文件不存在")

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))

            # 如果已有配置，返回提示
            if state.get("project_info", {}).get("writing_style"):
                return {"initialized": False, "message": "风格配置已存在"}

            # 根据题材获取默认配置
            genre = state.get("project_info", {}).get("genre", "")
            default_config = _get_default_style_config(genre)

            # 应用用户自定义
            default_config["primary"] = request.primary or default_config["primary"]
            default_config["intelligence"]["enabled"] = request.intelligence_enabled if request.intelligence_enabled is not None else False

            state["project_info"]["writing_style"] = default_config
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

            return {"initialized": True, "config": default_config}
        except Exception as e:
            raise HTTPException(500, f"初始化风格配置失败: {e}")

    class ToggleIntelligenceRequest(BaseModel):
        enabled: bool

    @app.post("/api/styles/toggle-intelligence")
    def toggle_intelligence(request: ToggleIntelligenceRequest):
        """快速开关智能风格切换"""
        state_path = _webnovel_dir() / "state.json"
        if not state_path.is_file():
            raise HTTPException(404, "项目状态文件不存在")

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))

            # 如果没有风格配置，自动初始化
            if "writing_style" not in state.get("project_info", {}):
                genre = state.get("project_info", {}).get("genre", "")
                state["project_info"]["writing_style"] = _get_default_style_config(genre)

            state["project_info"]["writing_style"]["intelligence"]["enabled"] = request.enabled
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

            return {"success": True, "enabled": request.enabled}
        except Exception as e:
            raise HTTPException(500, f"切换智能风格失败: {e}")
    # ===========================================================

    @app.get("/api/events")
    async def sse():
        """Server-Sent Events 端点，推送 .webnovel/ 下的文件变更。"""
        q = _watcher.subscribe()

        async def _gen():
            try:
                while True:
                    msg = await q.get()
                    yield f"data: {msg}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                _watcher.unsubscribe(q)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    # ===========================================================
    # 前端静态文件托管
    # ===========================================================

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

        @app.get("/{full_path:path}")
        def serve_spa(full_path: str):
            """SPA fallback：任何非 /api 路径都返回 index.html。"""
            index = STATIC_DIR / "index.html"
            if index.is_file():
                return FileResponse(str(index))
            raise HTTPException(404, "前端尚未构建")
    else:
        @app.get("/")
        def no_frontend():
            return HTMLResponse(
                "<h2>Webnovel Dashboard API is running</h2>"
                "<p>前端尚未构建。请先在 <code>dashboard/frontend</code> 目录执行 <code>npm run build</code>。</p>"
                '<p>API 文档：<a href="/docs">/docs</a></p>'
            )

    return app


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _walk_tree(folder: Path, root: Path) -> list[dict]:
    items = []
    for child in sorted(folder.iterdir()):
        rel = str(child.relative_to(root)).replace("\\", "/")
        if child.is_dir():
            items.append({"name": child.name, "type": "dir", "path": rel, "children": _walk_tree(child, root)})
        else:
            items.append({"name": child.name, "type": "file", "path": rel, "size": child.stat().st_size})
    return items


def _is_child(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
