"""Сайт личного бренда: страницы, форма заявок и админ-панель.

Запуск: python app.py
Сайт откроется на http://127.0.0.1:5050
"""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError
from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from config import Config
from content import (
    ABOUT,
    ASSISTANT,
    CASES,
    CONTACT,
    DATA_RECONCILIATION,
    FAQS,
    HERO,
    MANICURE_ASSISTANT,
    SAFETY_BRIEFING,
    SERVICES,
    SITE_DESCRIPTION,
    SITE_NAME,
    WORKFLOW,
    get_case,
    get_neighbors,
    is_draft_case,
)
from extensions import csrf, db, login_manager
from faq_assistant import FaqError, answer_question
from forms import ContactForm, LoginForm
from models import ContactRequest, User


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    Config.validate_production()

    os.makedirs(app.config["DATABASE_DIR"], exist_ok=True)
    os.makedirs(app.config["LOG_DIR"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    _setup_logging(app)
    _register_routes(app)
    _register_errors(app)

    with app.app_context():
        db.create_all()
        _ensure_admin(app)

    return app


def _setup_logging(app: Flask) -> None:
    """Пишет основные действия в logs/app.log и не добавляет второй такой же обработчик."""
    log_path = os.path.abspath(os.path.join(app.config["LOG_DIR"], "app.log"))
    for handler in app.logger.handlers:
        if getattr(handler, "baseFilename", None) == log_path:
            return

    file_handler = RotatingFileHandler(log_path, maxBytes=512_000, backupCount=2, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)


def _ensure_admin(app: Flask) -> None:
    """Создаёт администратора при первом запуске.

    Если в окружении задан ADMIN_PASSWORD и он не совпадает с сохранённым,
    пароль обновляется. Так смена пароля не требует ручной правки базы.
    """
    username = app.config["ADMIN_USERNAME"]
    password = app.config["ADMIN_PASSWORD"]
    user = db.session.scalar(select(User).where(User.username == username))

    if user is None:
        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        app.logger.info("Создана учётная запись администратора: %s", username)
    elif os.environ.get("ADMIN_PASSWORD") and not user.check_password(password):
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        app.logger.info("Пароль администратора обновлён из переменной окружения")

    if password == "admin123":
        app.logger.warning(
            "Используется пароль администратора по умолчанию. "
            "Задайте ADMIN_PASSWORD, чтобы сменить его."
        )


def _is_safe_url(target: str) -> bool:
    """Разрешает возврат после входа только на этот же сайт, а не на чужой адрес."""
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in {"http", "https"} and host_url.netloc == redirect_url.netloc


def _register_errors(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    @app.errorhandler(CSRFError)
    def handle_csrf(_error):
        app.logger.info("Форма отклонена: не совпал CSRF-токен")
        flash("Сессия формы устарела. Обновите страницу и отправьте её ещё раз.", "warning")
        return redirect(request.referrer or url_for("index"))


def _register_routes(app: Flask) -> None:
    public_endpoints = {
        "index",
        "assistant",
        "manicure_assistant",
        "safety_briefing",
        "data_reconciliation_assistant",
        "cases",
        "contact",
    }

    def public_url(endpoint: str, **values) -> str | None:
        """Строит абсолютный адрес только из явно заданного публичного домена."""
        base_url = app.config["PUBLIC_BASE_URL"]
        if not base_url:
            return None
        return base_url + url_for(endpoint, **values)

    @app.context_processor
    def inject_globals():
        unread = 0
        if current_user.is_authenticated:
            unread = db.session.scalar(
                select(func.count())
                .select_from(ContactRequest)
                .where(ContactRequest.is_read.is_(False))
            ) or 0
        endpoint = request.endpoint or ""
        page_is_public = endpoint in public_endpoints
        page_is_indexable = bool(app.config["SITE_INDEXABLE"] and page_is_public)
        canonical_url = public_url(endpoint) if page_is_public else None
        schema = []
        if page_is_indexable and canonical_url:
            schema = [
                {
                    "@context": "https://schema.org",
                    "@type": "Person",
                    "name": SITE_NAME,
                    "url": public_url("index"),
                    "email": CONTACT["email"],
                    "sameAs": [CONTACT["telegram_url"]],
                    "image": public_url("static", filename="images/natalya-sidorchuk.png"),
                },
                {
                    "@context": "https://schema.org",
                    "@type": "WebSite",
                    "name": SITE_NAME,
                    "url": public_url("index"),
                    "inLanguage": "ru-RU",
                },
            ]
        return {
            "site_name": SITE_NAME,
            "site_description": SITE_DESCRIPTION,
            "assistant": ASSISTANT,
            "contact_info": CONTACT,
            "current_year": datetime.now().year,
            "unread_count": unread,
            "canonical_url": canonical_url,
            "og_image_url": (
                public_url(
                    "static",
                    filename=(SAFETY_BRIEFING["image"] if endpoint == "safety_briefing" else ASSISTANT["image"]),
                )
                if page_is_indexable
                else None
            ),
            "page_robots": "index, follow" if page_is_indexable else "noindex, nofollow",
            "schema": schema,
        }

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith(("/admin", "/api/")):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.get("/robots.txt")
    def robots():
        if app.config["SITE_INDEXABLE"]:
            sitemap_url = public_url("sitemap")
            lines = [
                "User-agent: *",
                "Disallow: /admin",
                "Disallow: /api/",
                f"Sitemap: {sitemap_url}",
            ]
        else:
            lines = ["User-agent: *", "Disallow: /"]
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        if not app.config["SITE_INDEXABLE"]:
            abort(404)
        locations = [
            public_url(endpoint)
            for endpoint in (
                "index",
                "assistant",
                "manicure_assistant",
                "safety_briefing",
                "data_reconciliation_assistant",
                "cases",
                "contact",
            )
        ]
        urls = "".join(f"  <url><loc>{xml_escape(location or '')}</loc></url>\n" for location in locations)
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{urls}"
            "</urlset>\n"
        )
        return Response(document, mimetype="application/xml")

    @app.post("/api/faq/chat")
    @csrf.exempt
    def faq_chat():
        payload = request.get_json(silent=True) or {}
        try:
            reply = answer_question(
                app.config["BASE_DIR"],
                str(payload.get("message") or ""),
                payload.get("history") or [],
                request.remote_addr or "",
            )
        except FaqError as error:
            return jsonify({"answer": error.message}), error.status
        except Exception:
            app.logger.exception("Виджет FAQ не получил ответ модели")
            return jsonify({"answer": "Не удалось получить ответ. Попробуйте ещё раз чуть позже."}), 502
        return jsonify({"answer": reply})

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            hero=HERO,
            about=ABOUT,
            services=SERVICES,
            workflow=WORKFLOW,
            faqs=FAQS,
            cases=CASES,
        )

    @app.route("/assistant")
    def assistant():
        return render_template("assistant.html", assistant=ASSISTANT)

    @app.route("/manicure-assistant")
    def manicure_assistant():
        return render_template("manicure_assistant.html", case=MANICURE_ASSISTANT)

    @app.route("/safety-briefing-assistant")
    def safety_briefing():
        return render_template(
            "case_detail.html",
            case=SAFETY_BRIEFING,
            previous=MANICURE_ASSISTANT,
            following=DATA_RECONCILIATION,
            case_note=(
                "В кейсе перечислены только фактически выполненные проверки. "
                "Рабочие инструкции и данные сотрудников не публикуются."
            ),
        )

    @app.route("/data-reconciliation-assistant")
    def data_reconciliation_assistant():
        return render_template(
            "case_detail.html",
            case=DATA_RECONCILIATION,
            previous=SAFETY_BRIEFING,
            following=None,
            case_note=(
                "Публичная версия работает только с синтетическими данными. "
                "Рабочие бухгалтерские выгрузки защищены авторизацией."
            ),
        )

    @app.route("/cases")
    def cases():
        return render_template("cases.html", cases=CASES)

    @app.route("/cases/<slug>")
    def case_detail(slug):
        # Старые демонстрационные адреса не оставляем в поиске: направляем в
        # актуальное портфолио, а рабочий кейс сохраняет свой исходный /assistant.
        if slug == ASSISTANT["slug"]:
            return redirect(url_for("assistant"), code=301)
        if slug == MANICURE_ASSISTANT["slug"]:
            return redirect(url_for("manicure_assistant"), code=301)
        if is_draft_case(slug):
            return redirect(url_for("cases"), code=301)
        case = get_case(slug)
        if case is None:
            abort(404)
        previous, following = get_neighbors(slug)
        return render_template(
            "case_detail.html",
            case=case,
            previous=previous,
            following=following,
        )

    @app.route("/contact", methods=["GET", "POST"])
    def contact():
        form = ContactForm()
        if form.validate_on_submit():
            item = ContactRequest(
                name=form.name.data,
                email=form.email.data,
                phone=form.phone.data or "",
                subject=form.subject.data,
                message=form.message.data,
            )
            db.session.add(item)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception("Не удалось сохранить заявку")
                flash("Не получилось сохранить заявку. Попробуйте ещё раз.", "danger")
                return render_template("contact.html", form=form)

            # В журнал попадает номер заявки, а не текст и контакты.
            app.logger.info("Сохранена заявка #%s", item.id)
            flash("Заявка отправлена. Спасибо — она появилась в списке обращений.", "contact-success")
            return redirect(url_for("contact"))

        return render_template("contact.html", form=form)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if current_user.is_authenticated:
            return redirect(url_for("admin_requests"))

        form = LoginForm()
        if form.validate_on_submit():
            username = form.username.data
            user = db.session.scalar(select(User).where(User.username == username))
            if user is None or not user.check_password(form.password.data):
                app.logger.info("Неудачная попытка входа: %s", username)
                flash("Неверный логин или пароль.", "danger")
            else:
                login_user(user)
                app.logger.info("Администратор вошёл: %s", username)
                next_url = request.args.get("next")
                if not next_url or not _is_safe_url(next_url):
                    next_url = url_for("admin_requests")
                return redirect(next_url)

        return render_template("admin/login.html", form=form)

    @app.post("/admin/logout")
    @login_required
    def admin_logout():
        app.logger.info("Администратор вышел: %s", current_user.username)
        logout_user()
        flash("Вы вышли из админ-панели.", "info")
        return redirect(url_for("index"))

    @app.route("/admin")
    @login_required
    def admin_requests():
        items = db.session.scalars(
            select(ContactRequest).order_by(ContactRequest.created_at.desc())
        ).all()
        return render_template("admin/requests.html", items=items)

    @app.post("/admin/requests/<int:request_id>/read")
    @login_required
    def mark_read(request_id):
        item = db.session.get(ContactRequest, request_id)
        if item is None:
            abort(404)
        if not item.is_read:
            item.is_read = True
            db.session.commit()
            app.logger.info("Заявка #%s отмечена прочитанной", item.id)
        return redirect(url_for("admin_requests"))

    @app.post("/admin/requests/<int:request_id>/delete")
    @login_required
    def delete_request(request_id):
        item = db.session.get(ContactRequest, request_id)
        if item is None:
            abort(404)
        deleted_id = item.id
        db.session.delete(item)
        db.session.commit()
        app.logger.info("Заявка #%s удалена", deleted_id)
        flash("Заявка удалена.", "success")
        return redirect(url_for("admin_requests"))


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.logger.info("Запуск на http://127.0.0.1:%s", port)
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
