# -*- coding: utf-8 -*-
"""
真实身份生成器（防风控）

设计立场：
  - OpenAI / Stripe 的 ML 风控会把"机器生成感"邮箱（如 bokboh28885e@xxx）聚类
    为高风险账号。改用"first.last + 出生年"格式（william.harrison82）显著降低
    聚类风险
  - first_name / last_name / birthdate 三元组**必须互相一致**：
      email = "william.harrison82@..."     ← 暗示 1982 出生
      first_name = "William"              ← 必须和 email 前缀对应
      last_name = "Harrison"
      birthdate = "1982-XX-XX"            ← 年份必须和 email 后缀一致
    否则 OpenAI 注册时填的姓名/生日和邮箱不一致会被风控盯上
  - 名字池来自 SSA 1980-2005 高频英文名，避免少见名（少见名也是机器生成特征）

参考：
  - chatgpt2api 项目 services/register/openai_register.py:152 _random_name
  - 我们项目 main.py 既有的 _random_name（更简陋，本服务取代它）
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from typing import Optional


# ── 名字池（来自 SSA 1980-2005 美国高频名，避免太罕见也避免太大众）─────


_FIRST_NAMES_MALE = (
    "James", "Michael", "William", "David", "Richard", "Joseph", "Thomas",
    "Charles", "Christopher", "Daniel", "Matthew", "Anthony", "Mark", "Donald",
    "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian", "George",
    "Edward", "Ronald", "Timothy", "Jason", "Jeffrey", "Ryan", "Jacob", "Gary",
    "Nicholas", "Eric", "Stephen", "Jonathan", "Larry", "Justin", "Scott",
    "Brandon", "Frank", "Benjamin", "Gregory", "Samuel", "Raymond", "Patrick",
    "Alexander", "Jack", "Dennis", "Jerry", "Tyler", "Aaron", "Henry", "Douglas",
    "Adam", "Peter", "Nathan", "Zachary", "Walter", "Kyle", "Harold", "Carl",
    "Jeremy", "Keith", "Roger", "Gerald", "Ethan", "Arthur", "Terry", "Christian",
    "Sean", "Lawrence", "Austin", "Joe", "Noah", "Jesse", "Albert", "Bryan",
    "Bruce", "Willie", "Jordan", "Dylan", "Alan", "Ralph", "Gabriel", "Roy",
    "Juan", "Wayne", "Eugene", "Logan", "Randy", "Louis", "Russell", "Vincent",
    "Philip", "Bobby", "Johnny", "Bradley",
)

_FIRST_NAMES_FEMALE = (
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan",
    "Jessica", "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Sandra", "Margaret",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda",
    "Melissa", "Deborah", "Stephanie", "Rebecca", "Laura", "Sharon", "Cynthia",
    "Kathleen", "Amy", "Shirley", "Angela", "Helen", "Anna", "Brenda", "Pamela",
    "Nicole", "Samantha", "Katherine", "Christine", "Emma", "Catherine", "Debra",
    "Virginia", "Rachel", "Carolyn", "Janet", "Maria", "Heather", "Diane", "Ruth",
    "Julie", "Olivia", "Joyce", "Victoria", "Ruby", "Lauren", "Judith", "Christina",
    "Kelly", "Joan", "Evelyn", "Judy", "Andrea", "Hannah", "Megan", "Cheryl",
    "Jacqueline", "Martha", "Madison", "Teresa", "Gloria", "Sara", "Janice",
    "Ann", "Kathryn", "Abigail", "Sophia", "Frances", "Jean", "Alice", "Judy",
    "Isabella", "Julia", "Grace", "Amber", "Denise", "Danielle", "Marilyn", "Beverly",
    "Charlotte", "Natalie", "Theresa", "Diana", "Brittany", "Doris",
)

_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson",
    "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez",
    "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis",
    "Robinson", "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres",
    "Nguyen", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall",
    "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Gomez", "Phillips",
    "Evans", "Turner", "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes",
    "Stewart", "Morris", "Morales", "Murphy", "Cook", "Rogers", "Gutierrez",
    "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey", "Reed", "Kelly", "Howard",
    "Ramos", "Kim", "Cox", "Ward", "Richardson", "Watson", "Brooks", "Chavez",
    "Wood", "James", "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes", "Price",
    "Alvarez", "Castillo", "Sanders", "Patel", "Myers", "Long", "Ross", "Foster",
    "Jimenez", "Powell", "Jenkins", "Perry", "Russell", "Sullivan", "Bell",
    "Coleman", "Butler", "Henderson", "Barnes", "Fisher", "Carroll", "Simmons",
    "Bryant", "Reynolds", "Hamilton", "Graham", "Sullivan",
)


@dataclass(frozen=True)
class Identity:
    """生成的身份三元组 + 派生字段。

    所有字段保证内部一致：
      - email_local 必含 first_name.lower + last_name.lower
      - email_local 后缀两位数字 = 出生年后两位
      - birthdate 年份与 email_local 后缀一致
    """
    first_name: str   # "William"
    last_name: str    # "Harrison"
    email_local: str  # "william.harrison82"
    birthdate: str    # "1982-04-15"  (ISO yyyy-mm-dd)
    full_name: str    # "William Harrison"

    def __post_init__(self) -> None:
        # 自洽性 sanity check（防止以后改了实现忘了断言）
        assert self.first_name.lower() in self.email_local, "email_local 必含 first_name"
        assert self.last_name.lower() in self.email_local, "email_local 必含 last_name"
        yy_from_email = self.email_local[-2:]
        yy_from_birth = self.birthdate.split("-")[0][-2:]
        assert yy_from_email == yy_from_birth, "邮箱年份后缀必须和 birthdate 一致"


def _random_birthdate(year_min: int = 1985, year_max: int = 2003) -> tuple[str, str]:
    """随机生日（默认 1985-2003，避免太年轻=可疑 也避免太老）。

    返回 (iso_date, yy_suffix) — yy_suffix 用于嵌入 email_local 后缀。
    """
    year = secrets.choice(range(year_min, year_max + 1))
    month = secrets.choice(range(1, 13))
    # 简化：日固定 1-28 避免月底闰年算法
    day = secrets.choice(range(1, 29))
    return f"{year:04d}-{month:02d}-{day:02d}", str(year)[-2:]


def _build_email_local(first: str, last: str, yy: str) -> str:
    """构造 william.harrison82 / will.smith05 风格的 email 前缀。

    格式：first.last + 出生年后两位（保留点分隔，OpenAI 接受这种格式）。
    避开纯 william82 / 全部大写等机器特征。
    """
    return f"{first.lower()}.{last.lower()}{yy}"


def generate_identity(
    *,
    gender: Optional[str] = None,
    year_min: int = 1985,
    year_max: int = 2003,
) -> Identity:
    """生成单个一致身份。

    Args:
        gender: 'm' / 'f' / None（None 时各 50% 概率）
        year_min/year_max: 出生年范围
    """
    g = (gender or secrets.choice(("m", "f"))).lower()
    if g not in ("m", "f"):
        raise ValueError(f"gender 必须是 'm' / 'f' / None，得到 {gender!r}")

    pool = _FIRST_NAMES_MALE if g == "m" else _FIRST_NAMES_FEMALE
    first = secrets.choice(pool)
    last = secrets.choice(_LAST_NAMES)
    birthdate, yy = _random_birthdate(year_min=year_min, year_max=year_max)
    email_local = _build_email_local(first, last, yy)

    return Identity(
        first_name=first,
        last_name=last,
        email_local=email_local,
        birthdate=birthdate,
        full_name=f"{first} {last}",
    )


def generate_unique_identities(
    n: int,
    *,
    gender: Optional[str] = None,
    year_min: int = 1985,
    year_max: int = 2003,
    max_attempts_per: int = 30,
) -> list[Identity]:
    """生成 n 个不重复 email_local 的身份。

    碰撞概率：92 个名 × 117 个姓 × 19 个年份 = 204k 组合，n=100 时碰撞概率 < 5%。
    用集合去重 + 重抽避免极少数碰撞（不会无限循环 — 每个上限 max_attempts_per）。
    """
    if n <= 0:
        return []
    seen: set[str] = set()
    out: list[Identity] = []
    for i in range(n):
        for _ in range(max_attempts_per):
            ident = generate_identity(gender=gender, year_min=year_min, year_max=year_max)
            if ident.email_local not in seen:
                seen.add(ident.email_local)
                out.append(ident)
                break
        else:
            # 极端情况：连续 max_attempts_per 次都撞了，放宽（极小概率）
            ident = generate_identity(gender=gender, year_min=year_min, year_max=year_max)
            out.append(ident)  # 允许重复，调用方自己决定怎么办
    return out


__all__ = [
    "Identity",
    "generate_identity",
    "generate_unique_identities",
]
