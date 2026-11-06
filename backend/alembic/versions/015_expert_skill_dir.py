"""expert skill dir

Revision ID: 015_expert_skill_dir
Revises: 014_chapter_review_notes
Create Date: 2026-06-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "015_expert_skill_dir"
down_revision = "014_chapter_review_notes"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _columns(table_name: str) -> set[str]:
    return {col["name"] for col in inspect(_bind()).get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("experts")
    if "skill_dir" not in columns:
        op.add_column("experts", sa.Column("skill_dir", sa.String(100), nullable=True))

    mappings = {
        "创意大师": "creative-master",
        "残酷大师": "brutal-critic",
        "情节转折大师": "plot-twister",
        "渲染大师": "sensory-renderer",
        "专业编辑": "professional-editor",
        "概括者": "summarizer",
    }
    for name, skill_dir in mappings.items():
        op.execute(
            sa.text(
                "UPDATE experts SET skill_dir = :skill_dir "
                "WHERE name = :name AND (skill_dir IS NULL OR skill_dir = '')"
            ).bindparams(name=name, skill_dir=skill_dir)
        )


def downgrade() -> None:
    if "skill_dir" in _columns("experts"):
        op.drop_column("experts", "skill_dir")
