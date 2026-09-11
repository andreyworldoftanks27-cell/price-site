from datetime import date, datetime

from sqlalchemy import (
    String, Numeric, Date, DateTime, ForeignKey, UniqueConstraint, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    materials: Mapped[list["Material"]] = relationship(back_populates="supplier")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    materials: Mapped[list["Material"]] = relationship(back_populates="category")


class Material(Base):
    """
    Одна позиция от одного поставщика.
    Уникальность по (название, поставщик) — так работает дедупликация:
    если позиция уже есть, новая цена идёт в price_history,
    а не создаётся дубликат материала.
    """
    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("name", "supplier_id", name="uq_material_supplier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    unit: Mapped[str] = mapped_column(String(50), default="шт")
    quantity: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)

    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))

    current_price: Mapped[float] = mapped_column(Numeric(12, 2))
    price_date: Mapped[date] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    supplier: Mapped["Supplier"] = relationship(back_populates="materials")
    category: Mapped["Category"] = relationship(back_populates="materials")
    price_history: Mapped[list["PriceHistory"]] = relationship(back_populates="material")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    price_date: Mapped[date] = mapped_column(Date)
    source_file: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    material: Mapped["Material"] = relationship(back_populates="price_history")
