import os
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "chave-temporaria-doce-pedido"
)

os.makedirs(app.instance_path, exist_ok=True)
DATABASE = os.path.join(app.instance_path, "doce_pedido.db")


def conectar_banco():
    conexao = sqlite3.connect(DATABASE)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    return conexao


def criar_tabelas():
    with conectar_banco() as conexao:
        conexao.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                telefone TEXT NOT NULL,
                data_cadastro TEXT NOT NULL DEFAULT (
                    datetime('now', 'localtime')
                )
            )
        """)

        conexao.execute("""
            CREATE TABLE IF NOT EXISTS encomendas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                data_evento TEXT NOT NULL,
                hora_evento TEXT,
                descricao_bolo TEXT,
                tema TEXT,
                recheio TEXT,
                topo TEXT,
                valor_bolo REAL NOT NULL DEFAULT 0,
                entrada REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDENTE',
                observacoes TEXT,
                data_cadastro TEXT NOT NULL DEFAULT (
                    datetime('now', 'localtime')
                ),
                FOREIGN KEY (cliente_id)
                    REFERENCES clientes(id)
            )
        """)

        conexao.execute("""
            CREATE TABLE IF NOT EXISTS itens_encomenda (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                encomenda_id INTEGER NOT NULL,
                categoria TEXT NOT NULL
                    CHECK (categoria IN ('DOCE', 'SALGADO')),
                descricao TEXT NOT NULL,
                quantidade INTEGER NOT NULL CHECK (quantidade > 0),
                valor_unitario REAL NOT NULL
                    CHECK (valor_unitario >= 0),
                FOREIGN KEY (encomenda_id)
                    REFERENCES encomendas(id) ON DELETE CASCADE
            )
        """)

        conexao.execute("""
            CREATE TABLE IF NOT EXISTS pagamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                encomenda_id INTEGER NOT NULL,
                valor REAL NOT NULL CHECK (valor > 0),
                forma_pagamento TEXT NOT NULL,
                data_pagamento TEXT NOT NULL,
                observacoes TEXT,
                data_registro TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (encomenda_id)
                    REFERENCES encomendas(id) ON DELETE CASCADE
            )
        """)

        # Migração segura para bancos criados antes do campo "topo".
        colunas = {
            coluna["name"]
            for coluna in conexao.execute(
                "PRAGMA table_info(encomendas)"
            ).fetchall()
        }
        if "topo" not in colunas:
            conexao.execute(
                "ALTER TABLE encomendas ADD COLUMN topo TEXT"
            )


criar_tabelas()


def dinheiro(valor):
    try:
        numero = Decimal(str(valor).strip().replace(",", "."))
        if not numero.is_finite():
            raise InvalidOperation
        return numero.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("Informe valores monetários válidos.")


def converter_data_evento(valor):
    """
    Aceita data digitada no formato brasileiro DD/MM/AAAA
    e também o formato ISO AAAA-MM-DD. Retorna um objeto date.
    """
    valor = (valor or "").strip()
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(valor, formato).date()
        except ValueError:
            continue
    raise ValueError("Informe uma data válida no formato DD/MM/AAAA, por exemplo 03/11/2026.")


def buscar_clientes():
    with conectar_banco() as conexao:
        return conexao.execute("""
            SELECT id, nome, telefone
            FROM clientes
            ORDER BY nome COLLATE NOCASE
        """).fetchall()


@app.template_filter("moeda")
def formatar_moeda(valor):
    numero = Decimal(str(valor or 0))
    texto = f"{numero:,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


@app.route("/")
def inicio():
    with conectar_banco() as conexao:
        total_clientes = conexao.execute(
            "SELECT COUNT(*) FROM clientes"
        ).fetchone()[0]
        total_encomendas = conexao.execute(
            "SELECT COUNT(*) FROM encomendas"
        ).fetchone()[0]
        pendentes = conexao.execute("""
            SELECT COUNT(*) FROM encomendas
            WHERE status = 'PENDENTE'
        """).fetchone()[0]

    return render_template(
        "index.html",
        total_clientes=total_clientes,
        total_encomendas=total_encomendas,
        pendentes=pendentes
    )


@app.route("/clientes")
def listar_clientes():
    with conectar_banco() as conexao:
        clientes = conexao.execute("""
            SELECT id, nome, telefone, data_cadastro
            FROM clientes
            ORDER BY nome COLLATE NOCASE
        """).fetchall()
    return render_template("clientes.html", clientes=clientes)


@app.route("/clientes/novo", methods=["GET", "POST"])
def novo_cliente():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        telefone = request.form.get("telefone", "").strip()

        if not nome or not telefone:
            flash("Preencha o nome e o telefone do cliente.", "erro")
        else:
            try:
                with conectar_banco() as conexao:
                    conexao.execute("""
                        INSERT INTO clientes (nome, telefone)
                        VALUES (?, ?)
                    """, (nome, telefone))
                flash("Cliente cadastrado com sucesso!", "sucesso")
                return redirect(url_for("listar_clientes"))
            except sqlite3.Error:
                app.logger.exception("Erro ao cadastrar cliente")
                flash("Não foi possível salvar o cliente.", "erro")

    return render_template(
        "clientes.html",
        clientes=buscar_clientes()
    )


@app.route("/encomendas")
def listar_encomendas():
    with conectar_banco() as conexao:
        encomendas = conexao.execute("""
            SELECT
                e.id,
                e.data_evento,
                e.hora_evento,
                e.status,
                c.nome AS cliente_nome,
                e.entrada,
                COALESCE((SELECT SUM(p.valor) FROM pagamentos p WHERE p.encomenda_id = e.id), 0) AS pagamentos_adicionais,
                e.entrada + COALESCE((SELECT SUM(p.valor) FROM pagamentos p WHERE p.encomenda_id = e.id), 0) AS total_pago,
                e.valor_bolo + COALESCE((
                    SELECT SUM(i.quantidade * i.valor_unitario)
                    FROM itens_encomenda i
                    WHERE i.encomenda_id = e.id
                ), 0) AS total
            FROM encomendas e
            JOIN clientes c ON c.id = e.cliente_id
            ORDER BY e.data_evento DESC, e.id DESC
        """).fetchall()

    return render_template("encomendas.html", encomendas=encomendas)


@app.route("/encomendas/nova", methods=["GET", "POST"])
def nova_encomenda():
    clientes = buscar_clientes()

    if request.method == "POST":
        cliente_id = request.form.get("cliente_id", "").strip()
        data_evento = request.form.get("data_evento", "").strip()
        hora_evento = request.form.get("hora_evento", "").strip()
        descricao_bolo = request.form.get("descricao_bolo", "").strip()
        tema = request.form.get("tema", "").strip()
        recheio = request.form.get("recheio", "").strip()
        topo = request.form.get("topo", "Simples").strip()
        observacoes = request.form.get("observacoes", "").strip()
        incluir_doces = request.form.get("incluir_doces", "sim")
        incluir_salgados = request.form.get("incluir_salgados", "sim")

        try:
            if not clientes:
                raise ValueError(
                    "Cadastre um cliente antes de criar uma encomenda."
                )
            if not cliente_id or not data_evento:
                raise ValueError(
                    "Selecione o cliente e informe a data do evento."
                )

            cliente_id = int(cliente_id)
            if not any(int(c["id"]) == cliente_id for c in clientes):
                raise ValueError("O cliente selecionado não é válido.")

            data_obj = converter_data_evento(data_evento)
            data_evento = data_obj.isoformat()
            if data_obj < date.today():
                raise ValueError(
                    "A data do evento não pode ser anterior a hoje."
                )

            if not hora_evento:
                raise ValueError("Selecione o horário do evento.")
            if topo not in ("Simples", "De luxo"):
                raise ValueError("Selecione um tipo válido de topo.")

            if incluir_doces not in ("sim", "nao") or incluir_salgados not in ("sim", "nao"):
                raise ValueError("Selecione Sim ou Não para doces e salgados.")

            valor_bolo = dinheiro(request.form.get("valor_bolo", "0"))
            entrada = dinheiro(request.form.get("entrada", "0"))
            if valor_bolo < 0 or entrada < 0:
                raise ValueError("Os valores não podem ser negativos.")

            categorias = request.form.getlist("categoria_item")
            descricoes = request.form.getlist("descricao_item")
            quantidades = request.form.getlist("quantidade_item")
            valores = request.form.getlist("valor_unitario_item")

            itens = []
            for categoria, descricao, quantidade, valor in zip(
                categorias, descricoes, quantidades, valores
            ):
                descricao = descricao.strip()
                if not descricao and not quantidade and not valor:
                    continue

                if categoria not in ("DOCE", "SALGADO"):
                    raise ValueError("Categoria de item inválida.")
                if not descricao or not quantidade or not valor:
                    raise ValueError(
                        "Complete produto, quantidade e preço de cada item."
                    )

                quantidade = int(quantidade)
                valor = dinheiro(valor)
                if quantidade <= 0 or valor < 0:
                    raise ValueError(
                        "A quantidade deve ser positiva e o preço não pode ser negativo."
                    )
                itens.append((categoria, descricao, quantidade, valor))

            if incluir_doces == "nao" and any(i[0] == "DOCE" for i in itens):
                raise ValueError("Remova os doces ou selecione Sim para incluir doces.")
            if incluir_salgados == "nao" and any(i[0] == "SALGADO" for i in itens):
                raise ValueError("Remova os salgados ou selecione Sim para incluí-los.")

            total = valor_bolo + sum(
                (Decimal(q) * v for _, _, q, v in itens),
                Decimal("0.00")
            )
            if entrada > total:
                raise ValueError("A entrada não pode ser maior que o valor total.")

            status = "PENDENTE" if entrada < total else "CONFIRMADA"

            with conectar_banco() as conexao:
                cursor = conexao.execute("""
                    INSERT INTO encomendas (
                        cliente_id, data_evento, hora_evento,
                        descricao_bolo, tema, recheio, topo,
                        valor_bolo, entrada, status, observacoes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cliente_id, data_evento, hora_evento,
                    descricao_bolo, tema, recheio, topo,
                    float(valor_bolo), float(entrada), status,
                    observacoes
                ))
                encomenda_id = cursor.lastrowid

                for categoria, descricao, quantidade, valor in itens:
                    conexao.execute("""
                        INSERT INTO itens_encomenda (
                            encomenda_id, categoria, descricao,
                            quantidade, valor_unitario
                        ) VALUES (?, ?, ?, ?, ?)
                    """, (
                        encomenda_id, categoria, descricao,
                        quantidade, float(valor)
                    ))

            flash("Encomenda cadastrada com sucesso!", "sucesso")
            return redirect(url_for("listar_encomendas"))

        except (ValueError, InvalidOperation) as erro:
            flash(str(erro), "erro")
        except sqlite3.Error:
            app.logger.exception("Erro ao cadastrar encomenda")
            flash("Não foi possível salvar a encomenda.", "erro")

    return render_template(
        "nova_encomenda.html",
        clientes=clientes,
        hoje=date.today().isoformat()
    )


@app.route("/encomendas/<int:encomenda_id>/editar", methods=["GET", "POST"])
def editar_encomenda(encomenda_id):
    clientes = buscar_clientes()
    with conectar_banco() as conexao:
        encomenda = conexao.execute("""
            SELECT * FROM encomendas WHERE id = ?
        """, (encomenda_id,)).fetchone()
        itens_existentes = conexao.execute("""
            SELECT id, categoria, descricao, quantidade, valor_unitario
            FROM itens_encomenda WHERE encomenda_id = ? ORDER BY id
        """, (encomenda_id,)).fetchall()

    if not encomenda:
        flash("Encomenda não encontrada.", "erro")
        return redirect(url_for("listar_encomendas"))

    if request.method == "POST":
        cliente_id = request.form.get("cliente_id", "").strip()
        data_evento = request.form.get("data_evento", "").strip()
        hora_evento = request.form.get("hora_evento", "").strip()
        descricao_bolo = request.form.get("descricao_bolo", "").strip()
        tema = request.form.get("tema", "").strip()
        recheio = request.form.get("recheio", "").strip()
        topo = request.form.get("topo", "Simples").strip()
        observacoes = request.form.get("observacoes", "").strip()
        incluir_doces = request.form.get("incluir_doces", "sim")
        incluir_salgados = request.form.get("incluir_salgados", "sim")
        try:
            if not cliente_id or not data_evento:
                raise ValueError("Selecione o cliente e informe a data da entrega.")
            cliente_id = int(cliente_id)
            if not any(int(c["id"]) == cliente_id for c in clientes):
                raise ValueError("O cliente selecionado não é válido.")
            data_obj = converter_data_evento(data_evento)
            data_evento = data_obj.isoformat()
            if data_obj < date.today():
                raise ValueError("A data do evento não pode ser anterior a hoje.")
            if not hora_evento or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", hora_evento):
                raise ValueError("Informe o horário no formato HH:MM, por exemplo 14:30.")
            if topo not in ("Simples", "De luxo"):
                raise ValueError("Selecione um tipo válido de topo.")
            if incluir_doces not in ("sim", "nao") or incluir_salgados not in ("sim", "nao"):
                raise ValueError("Selecione Sim ou Não para doces e salgados.")
            valor_bolo = dinheiro(request.form.get("valor_bolo", "0"))
            entrada = dinheiro(request.form.get("entrada", "0"))
            if valor_bolo < 0 or entrada < 0:
                raise ValueError("Os valores não podem ser negativos.")

            categorias = request.form.getlist("categoria_item")
            descricoes = request.form.getlist("descricao_item")
            quantidades = request.form.getlist("quantidade_item")
            valores = request.form.getlist("valor_unitario_item")
            itens = []
            for categoria, descricao, quantidade, valor in zip(categorias, descricoes, quantidades, valores):
                descricao = descricao.strip()
                if not descricao and not quantidade and not valor:
                    continue
                if categoria not in ("DOCE", "SALGADO") or not descricao or not quantidade or not valor:
                    raise ValueError("Complete produto, quantidade e preço de cada item.")
                quantidade = int(quantidade)
                valor = dinheiro(valor)
                if quantidade <= 0 or valor < 0:
                    raise ValueError("A quantidade deve ser positiva e o preço não pode ser negativo.")
                itens.append((categoria, descricao, quantidade, valor))
            if incluir_doces == "nao" and any(i[0] == "DOCE" for i in itens):
                raise ValueError("Remova os doces ou selecione Sim para incluir doces.")
            if incluir_salgados == "nao" and any(i[0] == "SALGADO" for i in itens):
                raise ValueError("Remova os salgados ou selecione Sim para incluí-los.")
            total = valor_bolo + sum((Decimal(q) * v for _, _, q, v in itens), Decimal("0.00"))
            if entrada > total:
                raise ValueError("A entrada não pode ser maior que o valor total.")
            with conectar_banco() as conexao:
                pagamentos_adicionais = Decimal(str(conexao.execute(
                    "SELECT COALESCE(SUM(valor), 0) FROM pagamentos WHERE encomenda_id = ?",
                    (encomenda_id,)
                ).fetchone()[0] or 0))
                total_pago = entrada + pagamentos_adicionais
                if total_pago > total:
                    raise ValueError(
                        "O novo valor da encomenda não pode ser menor que o total já pago. "
                        "Revise a entrada ou o valor dos itens."
                    )
                status = "QUITADA" if total_pago >= total else "PENDENTE"
                conexao.execute("""UPDATE encomendas SET
                    cliente_id=?, data_evento=?, hora_evento=?, descricao_bolo=?,
                    tema=?, recheio=?, topo=?, valor_bolo=?, entrada=?, status=?, observacoes=?
                    WHERE id=?""", (cliente_id, data_evento, hora_evento, descricao_bolo,
                    tema, recheio, topo, float(valor_bolo), float(entrada), status,
                    observacoes, encomenda_id))
                conexao.execute("DELETE FROM itens_encomenda WHERE encomenda_id=?", (encomenda_id,))
                for categoria, descricao, quantidade, valor in itens:
                    conexao.execute("""INSERT INTO itens_encomenda
                        (encomenda_id, categoria, descricao, quantidade, valor_unitario)
                        VALUES (?, ?, ?, ?, ?)""", (encomenda_id, categoria, descricao, quantidade, float(valor)))
            flash("Encomenda atualizada com sucesso!", "sucesso")
            return redirect(url_for("listar_encomendas"))
        except (ValueError, InvalidOperation) as erro:
            flash(str(erro), "erro")
        except sqlite3.Error:
            app.logger.exception("Erro ao atualizar encomenda")
            flash("Não foi possível atualizar a encomenda.", "erro")

    return render_template("editar_encomenda.html", clientes=clientes,
        encomenda=encomenda, itens=itens_existentes, hoje=date.today().isoformat())


@app.route("/encomendas/<int:encomenda_id>/excluir", methods=["POST"])
def excluir_encomenda(encomenda_id):
    try:
        with conectar_banco() as conexao:
            existente = conexao.execute("SELECT id FROM encomendas WHERE id=?", (encomenda_id,)).fetchone()
            if not existente:
                flash("Encomenda não encontrada.", "erro")
            else:
                conexao.execute("DELETE FROM itens_encomenda WHERE encomenda_id=?", (encomenda_id,))
                conexao.execute("DELETE FROM encomendas WHERE id=?", (encomenda_id,))
                flash("Encomenda excluída com sucesso.", "sucesso")
    except sqlite3.Error:
        app.logger.exception("Erro ao excluir encomenda")
        flash("Não foi possível excluir a encomenda.", "erro")
    return redirect(url_for("listar_encomendas"))


@app.route("/encomendas/<int:encomenda_id>/pagamentos", methods=["GET", "POST"])
def pagamentos_encomenda(encomenda_id):
    formas = ("PIX", "Dinheiro", "Cartão de débito", "Cartão de crédito", "Transferência", "Outro")
    with conectar_banco() as conexao:
        encomenda = conexao.execute("""
            SELECT e.id, e.data_evento, e.hora_evento, e.entrada, c.nome AS cliente_nome,
                   COALESCE(e.valor_bolo, 0) + COALESCE((
                       SELECT SUM(i.quantidade * i.valor_unitario)
                       FROM itens_encomenda i WHERE i.encomenda_id = e.id
                   ), 0) AS total,
                   COALESCE((SELECT SUM(p.valor) FROM pagamentos p WHERE p.encomenda_id = e.id), 0) AS pagamentos_adicionais
            FROM encomendas e JOIN clientes c ON c.id = e.cliente_id
            WHERE e.id = ?
        """, (encomenda_id,)).fetchone()
        historico = conexao.execute("""
            SELECT id, valor, forma_pagamento, data_pagamento, observacoes, 'Pagamento' AS tipo
            FROM pagamentos WHERE encomenda_id = ?
            ORDER BY data_pagamento DESC, id DESC
        """, (encomenda_id,)).fetchall()

    if not encomenda:
        flash("Encomenda não encontrada.", "erro")
        return redirect(url_for("listar_encomendas"))

    total = Decimal(str(encomenda["total"] or 0))
    entrada = Decimal(str(encomenda["entrada"] or 0))
    adicionais = Decimal(str(encomenda["pagamentos_adicionais"] or 0))
    total_pago = entrada + adicionais
    saldo = max(total - total_pago, Decimal("0.00"))

    if request.method == "POST":
        try:
            valor = dinheiro(request.form.get("valor", ""))
            forma = request.form.get("forma_pagamento", "").strip()
            data_bruta = request.form.get("data_pagamento", "").strip()
            observacoes = request.form.get("observacoes", "").strip()
            if valor <= 0:
                raise ValueError("O valor do pagamento deve ser maior que zero.")
            if forma not in formas:
                raise ValueError("Selecione uma forma de pagamento válida.")
            try:
                data_pagamento = datetime.strptime(data_bruta, "%d/%m/%Y").date().isoformat()
            except ValueError:
                try:
                    data_pagamento = date.fromisoformat(data_bruta).isoformat()
                except ValueError:
                    raise ValueError("Informe a data do pagamento no formato DD/MM/AAAA.")
            if valor > saldo:
                raise ValueError(f"O pagamento excede o saldo devedor de R$ {saldo:.2f}.")
            with conectar_banco() as conexao:
                conexao.execute("""
                    INSERT INTO pagamentos (encomenda_id, valor, forma_pagamento, data_pagamento, observacoes)
                    VALUES (?, ?, ?, ?, ?)
                """, (encomenda_id, float(valor), forma, data_pagamento, observacoes))
                novo_pago = total_pago + valor
                novo_status = "QUITADA" if novo_pago >= total else "PENDENTE"
                conexao.execute("UPDATE encomendas SET status = ? WHERE id = ?", (novo_status, encomenda_id))
            flash("Pagamento registrado com sucesso!", "sucesso")
            return redirect(url_for("pagamentos_encomenda", encomenda_id=encomenda_id))
        except (ValueError, InvalidOperation) as erro:
            flash(str(erro), "erro")
        except sqlite3.Error:
            app.logger.exception("Erro ao registrar pagamento")
            flash("Não foi possível registrar o pagamento.", "erro")

    return render_template(
        "pagamentos_encomenda.html", encomenda=encomenda, historico=historico,
        formas=formas, total=total, entrada=entrada, total_pago=total_pago,
        saldo=saldo, hoje=date.today().strftime("%d/%m/%Y")
    )


@app.route("/calendario")
def calendario():
    """Calendário de entregas por dia, semana ou mês."""
    from datetime import timedelta
    import calendar as calendario_lib

    visao = request.args.get("visao", "mes").lower()
    if visao not in ("dia", "semana", "mes"):
        visao = "mes"

    try:
        selecionada = date.fromisoformat(request.args.get("data", ""))
    except (TypeError, ValueError):
        selecionada = date.today()

    if visao == "dia":
        inicio = selecionada
        fim = selecionada
        anterior = selecionada - timedelta(days=1)
        proximo = selecionada + timedelta(days=1)
        titulo_periodo = selecionada.strftime("%d/%m/%Y")
    elif visao == "semana":
        inicio = selecionada - timedelta(days=selecionada.weekday())
        fim = inicio + timedelta(days=6)
        anterior = inicio - timedelta(days=7)
        proximo = inicio + timedelta(days=7)
        titulo_periodo = f"{inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}"
    else:
        inicio = selecionada.replace(day=1)
        ultimo_dia = calendario_lib.monthrange(selecionada.year, selecionada.month)[1]
        fim = selecionada.replace(day=ultimo_dia)
        mes_anterior = (inicio - timedelta(days=1)).replace(day=1)
        mes_seguinte = (fim + timedelta(days=1)).replace(day=1)
        anterior = mes_anterior
        proximo = mes_seguinte
        titulo_periodo = selecionada.strftime("%B de %Y")

    with conectar_banco() as conexao:
        encomendas = conexao.execute("""
            SELECT e.id, e.data_evento, e.hora_evento, e.status,
                   e.entrada, c.nome AS cliente_nome,
                   e.valor_bolo + COALESCE((
                       SELECT SUM(i.quantidade * i.valor_unitario)
                       FROM itens_encomenda i WHERE i.encomenda_id = e.id
                   ), 0) AS total
            FROM encomendas e
            JOIN clientes c ON c.id = e.cliente_id
            WHERE e.data_evento >= ? AND e.data_evento <= ?
            ORDER BY e.data_evento,
                     CASE WHEN e.hora_evento IS NULL OR e.hora_evento = '' THEN 1 ELSE 0 END,
                     e.hora_evento, c.nome COLLATE NOCASE
        """, (inicio.isoformat(), fim.isoformat())).fetchall()

    por_data = {}
    for encomenda in encomendas:
        por_data.setdefault(encomenda["data_evento"], []).append(encomenda)

    dias = []
    dia_atual = inicio
    while dia_atual <= fim:
        chave = dia_atual.isoformat()
        dias.append({
            "data": dia_atual,
            "encomendas": por_data.get(chave, []),
            "total_dia": sum(float(e["total"] or 0) for e in por_data.get(chave, [])),
        })
        dia_atual += timedelta(days=1)

    return render_template(
        "calendario.html", visao=visao, selecionada=selecionada,
        inicio=inicio, fim=fim, anterior=anterior, proximo=proximo,
        titulo_periodo=titulo_periodo, dias=dias,
        total_encomendas=len(encomendas),
        total_valor=sum(float(e["total"] or 0) for e in encomendas)
    )


@app.route("/relatorios")
def relatorios():
    """Resumo financeiro e de encomendas por período da data do evento."""
    hoje = date.today()
    inicio_padrao = hoje.replace(day=1).isoformat()
    fim_padrao = hoje.isoformat()
    inicio = request.args.get("inicio", inicio_padrao).strip()
    fim = request.args.get("fim", fim_padrao).strip()

    try:
        inicio_obj = date.fromisoformat(inicio)
        fim_obj = date.fromisoformat(fim)
        if inicio_obj > fim_obj:
            raise ValueError
    except ValueError:
        inicio = inicio_padrao
        fim = fim_padrao
        flash("Período inválido. Foi exibido o período do mês atual.", "erro")

    with conectar_banco() as conexao:
        encomendas = conexao.execute("""
            SELECT
                e.id,
                e.data_evento,
                e.hora_evento,
                e.status,
                c.nome AS cliente_nome,
                COALESCE(e.valor_bolo, 0) + COALESCE((
                    SELECT SUM(i.quantidade * i.valor_unitario)
                    FROM itens_encomenda i
                    WHERE i.encomenda_id = e.id
                ), 0) AS total,
                COALESCE(e.entrada, 0) AS entrada,
                COALESCE((SELECT SUM(p.valor) FROM pagamentos p WHERE p.encomenda_id = e.id), 0) AS pagamentos_adicionais,
                COALESCE(e.entrada, 0) + COALESCE((SELECT SUM(p.valor) FROM pagamentos p WHERE p.encomenda_id = e.id), 0) AS total_pago
            FROM encomendas e
            JOIN clientes c ON c.id = e.cliente_id
            WHERE e.data_evento >= ? AND e.data_evento <= ?
            ORDER BY e.data_evento ASC, e.hora_evento ASC, c.nome COLLATE NOCASE
        """, (inicio, fim)).fetchall()

    total_encomendas = len(encomendas)
    valor_total = sum((Decimal(str(e["total"] or 0)) for e in encomendas), Decimal("0.00"))
    recebido = sum((Decimal(str(e["total_pago"] or 0)) for e in encomendas), Decimal("0.00"))
    saldo_pendente = sum((max(Decimal(str(e["total"] or 0)) - Decimal(str(e["total_pago"] or 0)), Decimal("0.00")) for e in encomendas), Decimal("0.00"))
    quantidade_pendentes = sum(
        1 for e in encomendas
        if Decimal(str(e["total"] or 0)) > Decimal(str(e["total_pago"] or 0))
    )

    return render_template(
        "relatorios.html",
        inicio=inicio,
        fim=fim,
        encomendas=encomendas,
        total_encomendas=total_encomendas,
        valor_total=valor_total,
        recebido=recebido,
        saldo_pendente=saldo_pendente,
        quantidade_pendentes=quantidade_pendentes,
    )


@app.route("/producao")
def lista_producao():
    """Lista de produção diária sem alterar os dados das encomendas."""
    data_param = request.args.get("data", "").strip()
    try:
        data_obj = date.fromisoformat(data_param) if data_param else date.today()
    except ValueError:
        data_obj = date.today()
        flash("Data inválida. Foi selecionado o dia de hoje.", "erro")

    data_iso = data_obj.isoformat()
    with conectar_banco() as conexao:
        encomendas = conexao.execute("""
            SELECT e.id, e.data_evento, e.hora_evento, e.status,
                   e.descricao_bolo, e.tema, e.recheio, e.topo,
                   e.observacoes, e.valor_bolo, c.nome AS cliente_nome,
                   c.telefone AS cliente_telefone
            FROM encomendas e
            JOIN clientes c ON c.id = e.cliente_id
            WHERE e.data_evento = ?
              AND UPPER(COALESCE(e.status, '')) NOT IN ('CANCELADA', 'CANCELADO')
            ORDER BY CASE WHEN COALESCE(e.hora_evento, '') = '' THEN 1 ELSE 0 END,
                     e.hora_evento, c.nome COLLATE NOCASE
        """, (data_iso,)).fetchall()

        itens = conexao.execute("""
            SELECT i.encomenda_id, i.categoria, i.descricao, i.quantidade,
                   i.valor_unitario
            FROM itens_encomenda i
            JOIN encomendas e ON e.id = i.encomenda_id
            WHERE e.data_evento = ?
              AND UPPER(COALESCE(e.status, '')) NOT IN ('CANCELADA', 'CANCELADO')
            ORDER BY i.categoria, i.descricao COLLATE NOCASE
        """, (data_iso,)).fetchall()

    itens_por_encomenda = {}
    totais_itens = {"DOCE": {}, "SALGADO": {}}
    for item in itens:
        itens_por_encomenda.setdefault(item["encomenda_id"], []).append(item)
        categoria = item["categoria"]
        descricao = item["descricao"]
        totais_itens.setdefault(categoria, {})[descricao] = (
            totais_itens.setdefault(categoria, {}).get(descricao, 0) + int(item["quantidade"] or 0)
        )

    bolos = [e for e in encomendas if (e["descricao_bolo"] or e["tema"] or e["recheio"] or e["topo"] or float(e["valor_bolo"] or 0) > 0)]
    return render_template(
        "producao.html", data_selecionada=data_obj,
        encomendas=encomendas, bolos=bolos,
        itens_por_encomenda=itens_por_encomenda,
        totais_doces=totais_itens.get("DOCE", {}),
        totais_salgados=totais_itens.get("SALGADO", {}),
        total_encomendas=len(encomendas),
        total_bolos=len(bolos),
        total_doces=sum(totais_itens.get("DOCE", {}).values()),
        total_salgados=sum(totais_itens.get("SALGADO", {}).values()),
    )


@app.route("/lembretes")
def lembretes_whatsapp():
    """Prepara lembretes de encomendas para enviar manualmente pelo WhatsApp."""
    data_param = request.args.get("data", "").strip()
    try:
        data_obj = date.fromisoformat(data_param) if data_param else date.today() + __import__("datetime").timedelta(days=2)
    except ValueError:
        data_obj = date.today() + __import__("datetime").timedelta(days=2)
        flash("Data inválida. Foram exibidos os lembretes para dois dias a partir de hoje.", "erro")

    with conectar_banco() as conexao:
        encomendas = conexao.execute("""
            SELECT e.id, e.data_evento, e.hora_evento, e.status,
                   COALESCE(e.valor_bolo, 0) + COALESCE((
                       SELECT SUM(i.quantidade * i.valor_unitario)
                       FROM itens_encomenda i WHERE i.encomenda_id = e.id
                   ), 0) AS total,
                   c.nome AS cliente_nome, c.telefone AS cliente_telefone
            FROM encomendas e
            JOIN clientes c ON c.id = e.cliente_id
            WHERE e.data_evento = ?
              AND UPPER(COALESCE(e.status, '')) NOT IN ('CANCELADA', 'CANCELADO')
            ORDER BY CASE WHEN COALESCE(e.hora_evento, '') = '' THEN 1 ELSE 0 END,
                     e.hora_evento, c.nome COLLATE NOCASE
        """, (data_obj.isoformat(),)).fetchall()

    lembretes = []
    for e in encomendas:
        telefone = re.sub(r"\D", "", e["cliente_telefone"] or "")
        if telefone.startswith("00"):
            telefone = telefone[2:]
        if len(telefone) in (10, 11):
            telefone = "55" + telefone
        telefone_valido = telefone.startswith("55") and len(telefone) in (12, 13)
        try:
            data_formatada = datetime.strptime(e["data_evento"], "%Y-%m-%d").strftime("%d/%m/%Y")
        except (TypeError, ValueError):
            data_formatada = e["data_evento"]
        horario = e["hora_evento"] or "horário a confirmar"
        total = Decimal(str(e["total"] or 0))
        total_texto = f"{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        mensagem = (
            f"Olá, {e['cliente_nome']}! 😊\n\n"
            f"Passando para lembrar que sua encomenda no Doce Pedido está agendada para "
            f"{data_formatada}, às {horario}.\n"
            f"Valor total da encomenda: R$ {total_texto}.\n\n"
            "Se precisar confirmar algum detalhe, estamos à disposição. Agradecemos pela preferência! 🍰"
        )
        url_whatsapp = f"https://wa.me/{telefone}?text={quote(mensagem, safe='')}" if telefone_valido else None
        lembretes.append({
            "id": e["id"], "cliente_nome": e["cliente_nome"],
            "telefone": e["cliente_telefone"], "data_evento": data_formatada,
            "hora_evento": e["hora_evento"], "status": e["status"],
            "total": total, "url_whatsapp": url_whatsapp,
        })

    return render_template(
        "lembretes.html", data_selecionada=data_obj,
        lembretes=lembretes, total_lembretes=len(lembretes)
    )


if __name__ == "__main__":
    app.run(debug=True)
