<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./images/readme-cover-dark.svg">
  <img alt="nanoinfra README cover" src="./images/readme-cover-light.svg">
</picture>

<div align="center">
  <p>
    <a href="https://github.com/nanoinfraorg/nanoinfra"><img src="https://img.shields.io/github/stars/nanoinfraorg/nanoinfra?style=flat&logo=github" alt="GitHub stars"></a>
    <a href="https://pypi.org/project/nanoinfra/"><img src="https://img.shields.io/pypi/v/nanoinfra" alt="PyPI version"></a>
    <a href="https://github.com/nanoinfraorg/nanoinfra/actions/workflows/ci.yml"><img src="https://github.com/nanoinfraorg/nanoinfra/actions/workflows/ci.yml/badge.svg?branch=main" alt="Test Suite"></a>
    <a href="https://pypi.org/project/nanoinfra/"><img src="https://img.shields.io/badge/python-%3E%3D3.11-blue" alt="Python 3.11 or newer"></a>
    <a href="https://github.com/nanoinfraorg/nanoinfra/blob/main/LICENSE"><img src="https://img.shields.io/github/license/nanoinfraorg/nanoinfra" alt="MIT License"></a>
  </p>
  <p>
    <a href="https://github.com/nanoinfraorg/nanoinfra/blob/main/COMMUNICATION.md">GitHub / Email</a>
  </p>
</div>

# nanoinfra

**nanoinfra** is an ultra-lightweight, open-source, self-hosted AI agent for infrastructure work, written in Python. It keeps an inventory of your servers, the credentials to reach them, and the tools to act on them over SSH, Ansible, AWS SSM, or an HTTP endpoint — reachable from a WebUI, a terminal, or sixteen chat apps. Tools, long-term memory, MCP integrations, model routing, multi-agent delegation, scheduled automation, and an OpenAI-compatible API sit in a core small enough to audit.

Root access, on a short leash: the agent reaches your hosts with credentials you hold, behind sender allowlists, a kernel sandbox, and mutating tools that default to a dry run — while running unprivileged itself.

Full docs, install instructions, and guides: see the [GitHub repository](https://github.com/nanoinfraorg/nanoinfra).
