"""
数据表结构定义 — SQLAlchemy ORM 模型

五张核心表:
  - BattleRecord    : 战报主表
  - BattleHero      : 单场战斗武将明细
  - Hero            : 武将基础信息表
  - BattleFormation : 阵容阵型表
  - SkillUsage      : 技能释放记录
"""

from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean,
    DateTime, ForeignKey, Text, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Hero(Base):
    """武将基础信息表 — 静态数据，手动/批量导入"""
    __tablename__ = "heroes"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    hero_id     = Column(Integer, unique=True, index=True, comment="游戏内武将ID")
    name        = Column(String(64), nullable=False, comment="武将名称")
    rarity      = Column(String(16), comment="稀有度 (SSR/SR/R/N)")
    troop_type  = Column(String(16), comment="兵种 (骑兵/枪兵/弓兵/盾兵)")
    faction     = Column(String(32), comment="所属势力/阵营")
    default_skills = Column(Text, comment="自带技能(JSON数组)")
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # 关联
    battle_appearances = relationship("BattleHero", back_populates="hero_info")

    def __repr__(self):
        return f"<Hero(id={self.hero_id}, name='{self.name}')>"


class BattleRecord(Base):
    """战报主表 — 每场战斗一条记录"""
    __tablename__ = "battle_records"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    battle_uid      = Column(String(128), unique=True, index=True, comment="战斗唯一ID(游戏生成)")
    timestamp       = Column(BigInteger, comment="战斗时间戳(ms)")
    time_iso        = Column(DateTime, comment="战斗时间(ISO)")
    attacker_id     = Column(String(128), comment="进攻方玩家ID")
    attacker_name   = Column(String(128), comment="进攻方玩家名")
    defender_id     = Column(String(128), comment="防守方玩家ID")
    defender_name   = Column(String(128), comment="防守方玩家名")
    result          = Column(String(32), comment="结果: attacker_win / defender_win / draw")
    total_rounds    = Column(Integer, comment="总回合数")
    battle_type     = Column(String(32), comment="战斗类型: pvp / pve / arena / legion")
    attacker_troops_total = Column(Integer, comment="进攻方总兵力")
    defender_troops_total = Column(Integer, comment="防守方总兵力")
    attacker_damage_total = Column(Integer, comment="进攻方总伤害")
    defender_damage_total = Column(Integer, comment="防守方总伤害")
    raw_filename    = Column(String(256), comment="来源原始文件名")
    raw_url         = Column(Text, comment="来源抓包URL")
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # 关联
    heroes      = relationship("BattleHero", back_populates="battle")
    formations  = relationship("BattleFormation", back_populates="battle")
    skills      = relationship("SkillUsage", back_populates="battle")

    def __repr__(self):
        return f"<Battle(uid='{self.battle_uid}', result='{self.result}')>"


class BattleHero(Base):
    """单场战斗武将明细 — 每场战斗每方最多 N 个武将"""
    __tablename__ = "battle_heroes"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    battle_id     = Column(Integer, ForeignKey("battle_records.id"), index=True, comment="关联战报ID")
    hero_id       = Column(Integer, ForeignKey("heroes.hero_id"), comment="关联武将ID")
    side          = Column(String(16), comment="阵营: attacker / defender")
    position      = Column(Integer, comment="站位 (1-6)")
    level         = Column(Integer, comment="等级")
    star          = Column(Integer, comment="星级")
    troops        = Column(Integer, comment="兵力")
    troops_lost   = Column(Integer, comment="损失兵力")
    damage_dealt  = Column(Integer, comment="造成伤害")
    damage_taken  = Column(Integer, comment="承受伤害")
    damage_healed = Column(Integer, comment="治疗量")
    kills         = Column(Integer, comment="击杀数")
    survived      = Column(Boolean, comment="是否存活")
    mvp           = Column(Boolean, default=False, comment="是否为MVP")

    # 关联
    battle    = relationship("BattleRecord", back_populates="heroes")
    hero_info = relationship("Hero", back_populates="battle_appearances")
    skills    = relationship("SkillUsage", back_populates="battle_hero")

    def __repr__(self):
        return f"<BattleHero(battle={self.battle_id}, hero={self.hero_id}, side='{self.side}')>"


class BattleFormation(Base):
    """阵容阵型表"""
    __tablename__ = "battle_formations"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    battle_id     = Column(Integer, ForeignKey("battle_records.id"), index=True)
    side          = Column(String(16), comment="阵营: attacker / defender")
    formation_name = Column(String(64), comment="阵型名称 (锋矢阵/鹤翼阵 等)")
    hero_ids      = Column(Text, comment="上阵武将ID列表(JSON数组)")
    hero_positions = Column(Text, comment="武将站位(JSON)")

    battle = relationship("BattleRecord", back_populates="formations")

    def __repr__(self):
        return f"<Formation(battle={self.battle_id}, side='{self.side}', name='{self.formation_name}')>"


class SkillUsage(Base):
    """技能释放记录"""
    __tablename__ = "skill_usages"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    battle_id       = Column(Integer, ForeignKey("battle_records.id"), index=True)
    battle_hero_id  = Column(Integer, ForeignKey("battle_heroes.id"), index=True)
    skill_name      = Column(String(128), comment="技能名称")
    skill_type      = Column(String(32), comment="技能类型: active / passive / tactic")
    casts           = Column(Integer, comment="释放次数")
    total_damage    = Column(Integer, comment="总伤害")
    total_heal      = Column(Integer, comment="总治疗")
    targets_hit     = Column(Integer, comment="命中目标数")

    battle      = relationship("BattleRecord", back_populates="skills")
    battle_hero = relationship("BattleHero", back_populates="skills")

    def __repr__(self):
        return f"<SkillUsage(battle={self.battle_id}, skill='{self.skill_name}')>"


def create_database(db_path: str = "data/database/battle_reports.db", echo: bool = False):
    """创建数据库及所有表"""
    from pathlib import Path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{db_path}", echo=echo)
    Base.metadata.create_all(engine)
    return engine


if __name__ == "__main__":
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/database/battle_reports.db"
    engine = create_database(db_path)
    print(f"数据库已创建: {db_path}")
    print(f"表列表: {list(Base.metadata.tables.keys())}")
