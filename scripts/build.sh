python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple && \
pip install pip-tools -i https://pypi.tuna.tsinghua.edu.cn/simple && \
pip-compile dependencies/requirements.in dependencies/dev-requirements.in -i https://pypi.tuna.tsinghua.edu.cn/simple --output-file requirements.txt && \
pip-sync requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple