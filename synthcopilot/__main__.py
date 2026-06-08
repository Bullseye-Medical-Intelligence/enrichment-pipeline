import sys

if "--gui" in sys.argv:
    from synthcopilot.gui import main
else:
    from synthcopilot.cli import main

if __name__ == "__main__":
    main()
