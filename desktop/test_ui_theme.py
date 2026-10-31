"""Shared styling stays idempotent across root and transient windows."""
import gc
import tkinter as tk
import unittest
from tkinter import ttk

from ui_theme import PALETTE, apply_theme


class ThemeTests(unittest.TestCase):
    def test_theme_is_consistent_on_root_and_child_without_changing_geometry(self):
        gc.collect()
        root = tk.Tk()
        root.withdraw()
        child = tk.Toplevel(root)
        child.withdraw()
        try:
            root.geometry("800x600+10000+10000")
            first = apply_theme(root)
            second = apply_theme(child)
            self.assertEqual(first, second)
            self.assertEqual(first, PALETTE)
            self.assertIsNot(first, PALETTE)
            self.assertEqual(root.cget("background"), PALETTE["bg"])
            style = ttk.Style(root)
            self.assertEqual(style.lookup("Card.TFrame", "background"), PALETTE["surface"])
            self.assertEqual(style.lookup("Primary.TButton", "foreground"), "white")
            self.assertGreaterEqual(int(style.lookup("Treeview", "rowheight")), 40)
        finally:
            child.destroy()
            root.destroy()
            root = child = None
            gc.collect()


if __name__ == "__main__":
    unittest.main()
