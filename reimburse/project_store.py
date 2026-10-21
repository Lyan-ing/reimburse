from __future__ import annotations

import shutil
import os
import uuid
from datetime import datetime
from pathlib import Path

from .project_models import Project, create_project, infer_name_from_folder, sanitize_filename


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.projects: list[Project] = []
        self.errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        self.projects = []
        self.errors = []
        self.root.mkdir(parents=True, exist_ok=True)
        seen: set[Path] = set()
        # Prune before traversal: old rglob walked every receipt/output/trash file.
        metadata = []
        for directory, children, files in os.walk(self.root, followlinks=False):
            relative = Path(directory).relative_to(self.root)
            depth = len(relative.parts)
            children[:] = sorted(
                child for child in children
                if depth < 3 and not child.startswith(".")
                and child not in {"_回收站", "_trash", "assets", "output"}
                and not (Path(directory) / child).is_symlink()
            )
            if "project.json" in files:
                metadata.append(Path(directory) / "project.json")
        for meta in sorted(metadata):
            try:
                rel_parts = meta.parent.relative_to(self.root).parts
            except ValueError:
                continue
            if not rel_parts or len(rel_parts) > 3:
                continue
            if any(part.startswith(".") or part in {"_回收站", "_trash", "assets", "output"} for part in rel_parts):
                continue
            resolved = meta.parent.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                project = Project.load(meta.parent)
                project.sort_records()
                self.projects.append(project)
            except Exception as exc:
                self.errors.append(f"{meta.parent.name}: {exc}")
        self.projects.sort(key=lambda p: (p.date_start or "", p.updated_at or ""), reverse=True)

    def create(
        self,
        name: str,
        project_type: str,
        date_start: str,
        date_end: str,
        applicant: str = "",
        department: str = "",
    ) -> Project:
        project = create_project(self.root, name, project_type, date_start, date_end, applicant, department)
        self.projects.insert(0, project)
        return project

    def register_folder(self, folder: Path, copy: bool = False) -> Project:
        folder = folder.resolve()
        existing_meta = folder.exists() and (folder / "project.json").exists()
        if existing_meta and not copy:
            project = Project.load(folder)
            if project not in self.projects:
                self.projects.insert(0, project)
            return project
        name, start, end = infer_name_from_folder(folder)
        if copy or self.root not in folder.parents:
            target = self.root / folder.name
            counter = 2
            while target.exists():
                target = self.root / f"{folder.name}_{counter}"
                counter += 1
            shutil.copytree(folder, target)
            folder = target
        if existing_meta:
            project = Project.load(folder)
            if project not in self.projects:
                self.projects.insert(0, project)
            return project
        project = Project(
            folder=folder,
            id=uuid.uuid4().hex[:12],
            name=name,
            type="其他",
            date_start=start,
            date_end=end,
            status="draft",
            notes="从已有文件夹导入，请补充费用记录。",
        )
        project.ensure_dirs()
        project.save()
        self.projects.insert(0, project)
        return project

    def remove_project(self, project: Project, move_to_trash: bool = True) -> None:
        self.projects = [p for p in self.projects if p.folder != project.folder]
        if not project.folder.exists():
            return
        if move_to_trash:
            trash = self.root / "_回收站"
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / f"{project.folder.name}_{datetime.now():%Y%m%d_%H%M%S}"
            shutil.move(str(project.folder), str(target))
        else:
            shutil.rmtree(project.folder, ignore_errors=True)

    def find(self, project_id: str) -> Project | None:
        for project in self.projects:
            if project.id == project_id:
                return project
        return None

    def filter(self, query: str = "", status: str = "all") -> list[Project]:
        query = query.strip().lower()
        result = []
        for project in self.projects:
            if status != "all" and project.status != status:
                continue
            if query:
                haystack = f"{project.name} {project.applicant} {project.department} {project.date_start} {project.date_end}".lower()
                if query not in haystack:
                    continue
            result.append(project)
        return result

    def status_counts(self) -> dict[str, int]:
        counts = {"all": len(self.projects)}
        for project in self.projects:
            counts[project.status] = counts.get(project.status, 0) + 1
        return counts
