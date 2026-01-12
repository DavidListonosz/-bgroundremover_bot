import os
import io
import time
import logging
import threading
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from rembg import remove, new_session
from PIL import Image

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MAX_FILE_SIZE_MB = 10
COOLDOWN_SECONDS = 20  # Задержка между запросами от одного пользователя
MAX_IMAGE_SIZE = 1920  # Максимальный размер стороны изображения

# Счетчики и ограничения
user_last_request = defaultdict(float)
total_processed = 0

# Модель будет загружена позже (после запуска HTTP сервера)
session = None

def get_session():
    """Ленивая загрузка модели - только когда нужна"""
    global session
    if session is None:
        logger.info("📥 Загружаю AI модель...")
        try:
            session = new_session("u2net_human_seg")
            logger.info("✅ Модель u2net_human_seg загружена")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось загрузить u2net_human_seg, используем стандартную: {e}")
            session = new_session("u2net")
    return session


def resize_if_large(image: Image.Image, max_size: int = MAX_IMAGE_SIZE) -> Image.Image:
    """Сжимает изображение если оно слишком большое"""
    if max(image.size) > max_size:
        ratio = max_size / max(image.size)
        new_size = tuple(int(dim * ratio) for dim in image.size)
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        logger.info(f"📐 Изображение изменено до {new_size}")
    return image


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 *Привет! Я бот для удаления фона с фотографий*\n\n"
        "📸 *Как использовать:*\n"
        "Просто отправь мне фото, и я удалю фон\n\n"
        "⚡ *Особенности:*\n"
        "• Бесплатно и без ограничений\n"
        "• Обработка 10-30 секунд\n"
        "• Результат в PNG с прозрачностью\n"
        "• Лучше всего работает с фото людей\n\n"
        "💡 *Совет:* Используй четкие фото с хорошим освещением\n\n"
        "❓ Команды:\n"
        "/start - это сообщение\n"
        "/stats - статистика бота\n"
        "/help - помощь"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = (
        "🆘 *Помощь*\n\n"
        "*Проблемы и решения:*\n\n"
        "❌ *Плохое качество результата?*\n"
        "→ Попробуй более четкое фото с лучшим освещением\n"
        "→ Бот лучше работает с фото людей\n\n"
        "❌ *Долго обрабатывается?*\n"
        "→ Первый запрос после простоя может занять до минуты\n"
        "→ Большие фото обрабатываются дольше\n\n"
        "❌ *Ошибка обработки?*\n"
        "→ Проверь размер фото (до 10 MB)\n"
        "→ Убедись что это изображение (JPG/PNG)\n\n"
        "📧 *Нашли баг?*\n"
        "Сообщи создателю: @your_username"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats"""
    stats_text = (
        f"📊 *Статистика бота*\n\n"
        f"🖼 Всего обработано: *{total_processed}* изображений\n"
        f"👤 Активных пользователей: *{len(user_last_request)}*\n\n"
        f"💚 Бот работает на бесплатном хостинге!"
    )
    await update.message.reply_text(stats_text, parse_mode='Markdown')


async def process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основная обработка фотографии"""
    global total_processed
    
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "пользователь"
    now = time.time()
    
    # Проверка cooldown
    time_since_last = now - user_last_request[user_id]
    if time_since_last < COOLDOWN_SECONDS:
        wait_time = int(COOLDOWN_SECONDS - time_since_last)
        await update.message.reply_text(
            f"⏱ Подожди еще *{wait_time} секунд* перед следующим запросом",
            parse_mode='Markdown'
        )
        return
    
    user_last_request[user_id] = now
    
    # Получаем информацию о фото
    photo = update.message.photo[-1]  # Берем самое большое фото
    file_size_mb = photo.file_size / (1024 * 1024)
    
    # Проверка размера файла
    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(
            f"❌ Файл слишком большой ({file_size_mb:.1f} MB)\n"
            f"Максимальный размер: {MAX_FILE_SIZE_MB} MB"
        )
        return
    
    status_msg = await update.message.reply_text("⏳ *Обрабатываю фото...*", parse_mode='Markdown')
    
    try:
        # Скачиваем фото
        logger.info(f"📥 Пользователь {user_name} ({user_id}) отправил фото {file_size_mb:.2f} MB")
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Открываем и оптимизируем изображение
        await status_msg.edit_text("🔄 *Подготовка изображения...*", parse_mode='Markdown')
        input_image = Image.open(io.BytesIO(photo_bytes))
        
        # Конвертируем в RGB если нужно
        if input_image.mode != 'RGB':
            input_image = input_image.convert('RGB')
        
        # Уменьшаем если слишком большое
        input_image = resize_if_large(input_image)
        
        # Удаляем фон
        await status_msg.edit_text("✨ *Удаляю фон... (это может занять 10-30 сек)*", parse_mode='Markdown')
        start_time = time.time()
        
        # Получаем сессию (модель загрузится при первом вызове)
        current_session = get_session()
        output_image = remove(input_image, session=current_session)
        
        processing_time = time.time() - start_time
        logger.info(f"⚡ Обработка заняла {processing_time:.1f} секунд")
        
        # Сохраняем результат
        output_bytes = io.BytesIO()
        output_image.save(output_bytes, format='PNG', optimize=True)
        output_bytes.seek(0)
        
        # Отправляем результат
        await status_msg.delete()
        
        caption = (
            f"✅ *Готово!*\n"
            f"⏱ Обработка: {processing_time:.1f} сек\n"
            f"📦 Формат: PNG с прозрачностью"
        )
        
        await update.message.reply_document(
            document=output_bytes,
            filename=f"no_bg_{user_id}_{int(now)}.png",
            caption=caption,
            parse_mode='Markdown'
        )
        
        total_processed += 1
        logger.info(f"✅ Успешно обработано. Всего: {total_processed}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки: {str(e)}", exc_info=True)
        await status_msg.edit_text(
            f"❌ *Произошла ошибка при обработке*\n\n"
            f"Попробуй:\n"
            f"• Отправить другое фото\n"
            f"• Фото меньшего размера\n"
            f"• Написать /help для подробностей",
            parse_mode='Markdown'
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов (если пользователь отправил фото как файл)"""
    await update.message.reply_text(
        "📎 *Ты отправил фото как документ*\n\n"
        "Для лучшего результата отправь фото *как фото* (не как файл)\n"
        "Просто нажми на скрепку → Фото и видео",
        parse_mode='Markdown'
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    logger.error(f"Update {update} вызвал ошибку {context.error}")


class HealthHandler(BaseHTTPRequestHandler):
    """Простой HTTP handler для health check"""
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    
    def log_message(self, format, *args):
        # Отключаем логи HTTP запросов
        pass


def start_http_server():
    """Запускает фейковый HTTP сервер для Render"""
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"🌐 HTTP server started on port {port}")
    server.serve_forever()


def main():
    """Запуск бота"""
    logger.info("🚀 Запуск бота...")
    logger.info(f"📍 TELEGRAM_TOKEN установлен: {bool(TELEGRAM_TOKEN)}")
    
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не установлен! Добавь его в переменные окружения.")
        # Всё равно запускаем HTTP сервер чтобы Render видел порт
        start_http_server()
        return
    
    logger.info("🌐 Запуск HTTP сервера...")
    # Запускаем HTTP сервер в отдельном потоке (для Render)
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    
    logger.info("🔧 Создание приложения...")
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.PHOTO, process_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    
    # Обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    logger.info("🤖 Бот запущен и готов к работе!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
