# SurrealDB-ORM

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![CI](https://github.com/EulogySnowfall/SurrealDB-ORM/actions/workflows/ci.yml/badge.svg)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM/graph/badge.svg?token=XUONTG2M6Z)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM)
![GitHub License](https://img.shields.io/github/license/EulogySnowfall/SurrealDB-ORM)

🚀 **SurrealDB-ORM** is a lightweight ORM (Object-Relational Mapping) inspired by Django ORM, designed to simplify interactions with **SurrealDB** in Python projects. It provides an intuitive way to manage models, perform queries, and execute CRUD (Create, Read, Update, Delete) operations.

---

## 📋 Table of Contents

- [Version](#-version)
- [Description](#-description)
- [Requirements and tested based](#-requirements-and-tested-based)
- [Installation](#-installation)
- [Usage Example](#-usage-example)
- [Features](#-features)
- [Contributing](#-contributing)
- [TODO](#-todo)
- [License](#-license)

---

## ✅ Version

Alpha 0.1.3

---

## 📝 Description

SurrealDB-ORM offers a clean abstraction for handling SurrealDB through Python models.  
The goal is to simplify writing complex queries while providing an intuitive interface similar to modern ORMs like Django or SQLAlchemy.

---

## 📝 Requirements and tested based

- Python : 3.11~3.13
- Pydantic : 2.10.4
- SurrealDB Database Version : 2.1.4
- You need to set a SurrealDB to connect to.  

---

## 🛠️ Installation

```bash
pip install surrealdb-orm
```

---

## 🚀 Usage Example

Here's a simple example demonstrating how to define a model and interact with SurrealDB:

### 1. Define a Model

```python
from surreal_orm.modelBase import BaseSurrealModel
from pydantic import BaseModel, Field
from typing import Optional


class User(BaseSurrealModel):
    id: Optional[str] = None
    name: str = Field(..., max_length=100)
    email: str
```

### 2. Create and Save a User

```python
user = User(name="Alice", email="alice@example.com")
await user.save()
```

### 3. Query Users

```python
users = await User.objects().filter(name="Alice").exec()
for user in users:
    print(user.name, user.email)
```

---

## 🌟 Features

- 🔧 **Model definition** using Pydantic  
- 📄 **QuerySet** with filter methods like `filter()`, `limit()`, and `order_by()`  
- 🔄 **CRUD** operations (Create, Read, Update, Delete)  
- ⚙️ **Asynchronous connection** to SurrealDB  
- 🔍 **Automatic validation** with Pydantic  
- 📊 **Complex queries** with conditional filters (`age__gte`, `name__in`, etc.)  

---

## 🤝 Contributing

Contributions are welcome!  
If you'd like to improve this project:

1. Fork the repository.  
2. Create a branch (`git checkout -b feature/new-feature`).  
3. Make your changes and commit them (`git commit -m "Add new feature"`).  
4. Push to your branch (`git push origin feature/new-feature`).  
5. Create a Pull Request.  

---

## 📌 TODO

- [ ] Implement relationships
- [ ] Add transaction support  
- [ ] Optimize complex queries  
- [ ] Expand documentation with advanced examples  
- [ ] Better SurrealQL Integration

---

## 📄 License

This project is licensed under the **MIT License**. See the `LICENSE` file for more information.

---

### 📨 Contact

**Author:** Yannick Croteau  
**Email:** <croteau.yannick@gmail.com>  
**GitHub:** [EulogySnowfall](https://github.com/EulogySnowfall)  

---

**SurrealDB-ORM** is a personal package to use with other projects. I will certainly improve it over the next few months, but feel free to open issues or suggest improvements 🚀
