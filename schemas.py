import re
from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    role: str = Field(min_length=1, max_length=24)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value):
        if not value.strip():
            raise ValueError("Name is required.")
        return value.strip()

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class SetupInstructorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value):
        if not value.strip():
            raise ValueError("Name is required.")
        return value.strip()

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value
