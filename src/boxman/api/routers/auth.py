"""Authentication and user/grant management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from boxman.api.auth import rbac, security
from boxman.api.auth.deps import get_current_user, require_admin
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.schemas.auth import GrantCreate, GrantOut, Token, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=Token, summary="obtain a JWT (OAuth2 password)")
def login(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> Token:
    user = db.execute(
        select(User).where(User.username == form.username)
    ).scalars().first()
    if (
        user is None
        or not user.is_active
        or not security.verify_password(form.password, user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Token(access_token=security.create_access_token(user.id, user.role))


@router.get("/me", response_model=UserOut, summary="current user")
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="create a user (admin)",
)
def create_user(req: UserCreate, db: Session = Depends(get_db)) -> User:
    if db.execute(select(User).where(User.username == req.username)).scalars().first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username exists")
    user = User(
        username=req.username,
        hashed_password=security.hash_password(req.password),
        role=req.role.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get(
    "/users",
    response_model=list[UserOut],
    dependencies=[Depends(require_admin)],
    summary="list users (admin)",
)
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return list(db.execute(select(User)).scalars().all())


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_admin)],
    summary="delete a user (admin)",
)
def delete_user(user_id: str, db: Session = Depends(get_db)) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{user_id}/grants",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="grant a user access to a project (admin)",
)
def add_grant(user_id: str, req: GrantCreate, db: Session = Depends(get_db)) -> object:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    grant = rbac.grant_project(db, user, req.project, req.role.value)
    db.commit()
    db.refresh(grant)
    return grant


@router.get(
    "/users/{user_id}/grants",
    response_model=list[GrantOut],
    dependencies=[Depends(require_admin)],
    summary="list a user's project grants (admin)",
)
def list_grants(user_id: str, db: Session = Depends(get_db)) -> list[object]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return list(user.grants)
