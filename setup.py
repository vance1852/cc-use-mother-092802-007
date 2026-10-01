from setuptools import find_packages, setup

setup(
    name="beverage-ops-foundation",
    version="0.2.0",
    description="技能赛训协作基础服务与蒸汽技改收益核验",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
