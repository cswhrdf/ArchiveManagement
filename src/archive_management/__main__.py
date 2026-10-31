"""支持 ``python -m archive_management`` 的运行入口."""

from archive_management.app import main

if __name__ == "__main__":  # pragma: no branch - 作为脚本跑时进, 被导入时不进
    main()
