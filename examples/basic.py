# Surrealdb ORM Examples

# That if for use as example with the uv VSCode plug-in. You can select a part of the code and execute it
# as cell with Shtft+Enter. (Jupyter notenook like)
# Use the comment as cell definition

# Load requirements

import os
from src.surreal_orm.model_base import BaseSurrealModel, SurrealDBConnectionManager
from pydantic import ConfigDict
from dotenv import load_dotenv
import asyncio


# Load environnement from .env (copy and rename .env.example in needed)
# type: ignore
load_dotenv()

# Initialiser SurrealDB and start your SurrealDB
SURREALDB_URL = os.getenv("SURREALDB_URL")
SURREALDB_USER = os.getenv("SURREALDB_USER")
SURREALDB_PASS = os.getenv("SURREALDB_PASS")
SURREALDB_NAMESPACE = os.getenv("SURREALDB_NAMESPACE")
SURREALDB_DATABASE = os.getenv("SURREALDB_DATABASE")

if all(
    [
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    ]
):
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


# Create class. The 'Extra allow', will give you the posibility to set ID on load data from the database
# if you don't have id in your Model
class User(BaseSurrealModel):
    model_config = ConfigDict(extra="allow", primary_key="email")
    name: str
    age: int
    email: str


async def create_user():
    # Create a new user
    user = User(name="John Doe", age=30, email="john.doe@example.com")
    await user.save()

    print("User created:", user)

    user2 = User(name="Jeanne Doe", age=28, email="jeanne.doe@example.com")
    await user2.save()

    print("User2 created:", user2)


async def get_users():
    users = await User.objects().all()

    for user_item in users:
        print(user_item.id)


async def get_user():
    user = await User.objects().filter(name="John Doe").first()
    print(user)


async def select_age_greater_than():
    users = await User.objects().filter(age__gt=30).exec()
    for user_item in users:
        print(user_item)


async def select_fields():
    users = await User.objects().select("name", "age").exec()
    for user_item in users:
        print(user_item)


async def delete_table():
    await User.objects().delete_table()
    print("Table deleted")


async def update_user():
    user = await User.objects().filter(name="John Doe").first()
    if user:
        user.age = 31
        await user.update()
        print("User updated:", user)
    else:
        print("User not found")


async def merge_user():
    user = await User.objects().get("john.doe@example.com")
    if user:
        await user.merge(age=32)
        print("User merged:", user)
    else:
        print("User not found")


async def delete_user():
    user = await User.objects().filter(name="John Doe").first()
    if user:
        await user.delete()
        print("User deleted:", user)
    else:
        print("User not found")


async def main():
    await create_user()
    await get_users()
    await update_user()
    await select_age_greater_than()
    await select_fields()
    await merge_user()
    await delete_user()
    await delete_table()


if __name__ == "__main__":
    # Run the async function
    asyncio.run(main())
