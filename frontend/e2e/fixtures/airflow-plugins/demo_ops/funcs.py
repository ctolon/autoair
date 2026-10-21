"""Functions referenced from DAGs built in AutoAir (Python references)."""


def greet(name: str = "world") -> str:
    print(f"hello {name}")
    return f"hello {name}"
