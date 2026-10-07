from pydantic import BaseModel


class SearchHit(BaseModel):
    category: str
    id: str
    title: str
    subtitle: str | None = None
    href: str
