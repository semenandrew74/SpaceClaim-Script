# Python Script, API Version = V252
# -*- coding: utf-8 -*-
# Запускалка: выполняет idealize.py из файла, не вставляя его текст в редактор.
# Зачем: редактор SpaceClaim при запуске выводит в окно Interpreter весь текст
# скрипта; на длинном скрипте это может надолго подвешивать интерфейс.
# Как пользоваться: положите idealize.py в "Документы" (или укажите свой путь ниже),
# откройте в Script Editor ЭТОТ файл и запускайте его.
import os
import System

SCRIPT = os.path.join(
    System.Environment.GetFolderPath(System.Environment.SpecialFolder.MyDocuments),
    "idealize.py")
# SCRIPT = r"D:\work\SpaceClaim-Script\idealize.py"   # свой путь - раскомментировать

execfile(SCRIPT, globals())
