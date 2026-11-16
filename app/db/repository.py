"""Repository layer: all template/job persistence goes through here (routers never build queries)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Job, Template, TemplateVersion

SortField = Literal["name", "created_at", "updated_at"]


@contextmanager
def transaction(db: Session) -> Iterator[Session]:
    """Commit on success, roll back on any error."""
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


class TemplateRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, template_id: int) -> Template | None:
        return self.db.get(Template, template_id)

    def get_by_name(self, name: str) -> Template | None:
        return self.db.scalar(select(Template).where(func.lower(Template.name) == name.lower()))

    def list(
        self,
        search: str | None = None,
        sort: SortField = "updated_at",
        descending: bool = True,
        include_archived: bool = False,
        archived_only: bool = False,
    ) -> list[Template]:
        q = select(Template)
        if archived_only:
            q = q.where(Template.is_archived.is_(True))
        elif not include_archived:
            q = q.where(Template.is_archived.is_(False))
        if search:
            like = f"%{search.lower()}%"
            q = q.where(or_(func.lower(Template.name).like(like), func.lower(Template.description).like(like)))
        col = getattr(Template, sort)
        q = q.order_by(col.desc() if descending else col.asc(), Template.id)
        return list(self.db.scalars(q))

    def unique_name(self, base: str) -> str:
        if not self.get_by_name(base):
            return base
        n = 2
        while self.get_by_name(f"{base} ({n})"):
            n += 1
        return f"{base} ({n})"

    def add(self, template: Template) -> Template:
        """Insert a template and its first version row."""
        template.version = 1
        self.db.add(template)
        self.db.flush()
        self._snapshot(template)
        return template

    def _snapshot(self, t: Template) -> TemplateVersion:
        v = TemplateVersion(template_id=t.id, version=t.version, config_json=t.config_json, image_ref=t.image_sha256)
        self.db.add(v)
        self.db.flush()
        return v

    def new_version(self, t: Template) -> TemplateVersion:
        """Bump the version number and store a snapshot of the current state."""
        t.version += 1
        return self._snapshot(t)

    def versions(self, t: Template) -> list[TemplateVersion]:
        return list(self.db.scalars(select(TemplateVersion).where(TemplateVersion.template_id == t.id).order_by(TemplateVersion.version.desc())))

    def version(self, t: Template, version: int) -> TemplateVersion | None:
        return self.db.scalar(select(TemplateVersion).where(TemplateVersion.template_id == t.id, TemplateVersion.version == version))

    def delete(self, t: Template) -> None:
        self.db.delete(t)
        self.db.flush()


class JobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, job_id: int) -> Job | None:
        return self.db.get(Job, job_id)

    def list(self, kind: str | None = None, limit: int = 200) -> list[Job]:
        q = select(Job).order_by(Job.id.desc()).limit(limit)
        if kind:
            q = q.where(Job.kind == kind)
        return list(self.db.scalars(q))

    def add(self, **fields: Any) -> Job:
        job = Job(**fields)
        self.db.add(job)
        self.db.flush()
        return job
