// ========================
// 1. ИНИЦИАЛИЗАЦИЯ ПРИЛОЖЕНИЯ
// ========================
require('dotenv').config();
const express = require('express');
const TelegramBot = require('node-telegram-bot-api');
const mongoose = require('mongoose');

// Проверка обязательных переменных окружения
const requiredEnvVars = ['MONGO_URI', 'TELEGRAM_TOKEN', 'ADMIN_IDS'];
const missingVars = requiredEnvVars.filter(v => !process.env[v]);

if (missingVars.length > 0) {
  console.error('❌ Отсутствуют обязательные переменные окружения:', missingVars.join(', '));
  process.exit(1);
}

// Настройка бота
const botOptions = {
  polling: true,
  request: {
    proxy: process.env.PROXY || null
  }
};

const bot = new TelegramBot(process.env.TELEGRAM_TOKEN, botOptions);
const app = express();
const PORT = process.env.PORT || 3000;

// ========================
// 2. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ
// ========================
const connectDB = async () => {
  try {
    await mongoose.connect(process.env.MONGO_URI, {
      useNewUrlParser: true,
      useUnifiedTopology: true
    });
    console.log('✅ Подключено к MongoDB');
  } catch (err) {
    console.error('❌ Ошибка подключения к MongoDB:', err.message);
    process.exit(1);
  }
};

connectDB();

// ========================
// 3. МОДЕЛИ ДАННЫХ
// ========================
const userSchema = new mongoose.Schema({
  telegramId: { type: Number, unique: true, required: true },
  name: { type: String, trim: true },
  phone: { type: String, match: /^\+?[0-9]{10,15}$/ },
  address: { type: String, trim: true },
  createdAt: { type: Date, default: Date.now }
});

const carSchema = new mongoose.Schema({
  brand: { type: String, required: true, trim: true },
  licensePlate: { type: String, required: true, unique: true, trim: true },
  capacity: { type: String, required: true },
  hoseLength: { type: String, required: true },
  schedule: {
    days: { type: [Number], required: true },
    hours: {
      start: { type: Number, required: true, min: 0, max: 23 },
      end: { type: Number, required: true, min: 1, max: 24 }
    }
  },
  isActive: { type: Boolean, default: true }
});

const orderSchema = new mongoose.Schema({
  user: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true },
  car: { type: mongoose.Schema.Types.ObjectId, ref: 'Car', required: true },
  datetime: { type: Date, required: true },
  status: { 
    type: String, 
    enum: ['new', 'confirmed', 'completed', 'canceled'],
    default: 'new' 
  },
  cancelReason: String,
  createdAt: { type: Date, default: Date.now }
});

const User = mongoose.model('User', userSchema);
const Car = mongoose.model('Car', carSchema);
const Order = mongoose.model('Order', orderSchema);

// ========================
// 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
// ========================
const isAdmin = (chatId) => {
  return process.env.ADMIN_IDS.split(',').map(id => id.trim()).includes(chatId.toString());
};

const formatDate = (date) => {
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Moscow'
  });
};

const MONTHS_SHORT_RU = ['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек'];

const formatHourSlotLabel = (hour) => {
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(hour)}:00-${pad(hour + 1)}:00`;
};

const waitForNextMessageFrom = (chatId, timeoutMs = 120000) => {
  return new Promise((resolve, reject) => {
    const handler = (msg) => {
      if (msg.chat && msg.chat.id === chatId && typeof msg.text === 'string') {
        cleanup();
        resolve(msg);
      }
    };
    const cleanup = () => {
      clearTimeout(timer);
      bot.removeListener('message', handler);
    };
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error('Время ожидания ответа истекло'));
    }, timeoutMs);
    bot.on('message', handler);
  });
};

const getAvailableHours = async (carId, selectedDate, workingHours) => {
  const startOfDay = new Date(selectedDate);
  startOfDay.setHours(0, 0, 0, 0);
  const endOfDay = new Date(selectedDate);
  endOfDay.setHours(23, 59, 59, 999);

  const existingOrders = await Order.find({
    car: carId,
    datetime: { $gte: startOfDay, $lte: endOfDay },
    status: { $in: ['new', 'confirmed'] }
  }).exec();

  const bookedHours = existingOrders.map(order => order.datetime.getHours());
  const availableHours = [];
  for (let hour = workingHours.start; hour < workingHours.end; hour++) {
    if (!bookedHours.includes(hour)) {
      availableHours.push(hour);
    }
  }
  return availableHours;
};

const isSameYMD = (d1, d2) => {
  return d1.getFullYear() === d2.getFullYear() && d1.getMonth() === d2.getMonth() && d1.getDate() === d2.getDate();
};

// ========================
// 5. ОСНОВНЫЕ МЕНЮ
// ========================
const userMainMenu = () => ({
  reply_markup: {
    keyboard: [
      ['🚗 Записаться на откачку'],
      ['📋 Мои заказы'],
      ['✏️ Редактировать профиль']
    ],
    resize_keyboard: true
  }
});

const adminMainMenu = () => ({
  reply_markup: {
    keyboard: [
      ['🚗 Управление автомобилями', '📋 Управление заказами'],
      ['👥 Управление клиентами', '📢 Рассылка'],
      ['📊 Статистика']
    ],
    resize_keyboard: true
  }
});

// ========================
// 6. ОБРАБОТКА КОМАНД ПОЛЬЗОВАТЕЛЯ
// ========================
bot.onText(/\/start/, async (msg) => {
  const chatId = msg.chat.id;
  
  try {
    let user = await User.findOne({ telegramId: chatId }).exec();
    
    if (!user) {
      user = new User({ telegramId: chatId });
      await user.save();
      await bot.sendMessage(chatId, 'Добро пожаловать! Заполните ваш профиль:');
      return askForUserDetails(chatId);
    }
    
    const options = isAdmin(chatId) ? adminMainMenu() : userMainMenu();
    await bot.sendMessage(chatId, 'Главное меню:', options);
  } catch (error) {
    console.error('Start command error:', error);
    await bot.sendMessage(chatId, '⚠️ Произошла ошибка. Попробуйте позже.');
  }
});

const askForUserDetails = async (chatId) => {
  try {
    await bot.sendMessage(chatId, 'Введите ваше ФИО:');
    const nameMsg = await waitForNextMessageFrom(chatId);

    await bot.sendMessage(chatId, 'Введите ваш телефон в формате +79991234567:');
    const phoneMsg = await waitForNextMessageFrom(chatId);

    await bot.sendMessage(chatId, 'Введите ваш адрес:');
    const addressMsg = await waitForNextMessageFrom(chatId);

    await User.updateOne(
      { telegramId: chatId },
      { 
        $set: { 
          name: nameMsg.text.trim(),
          phone: phoneMsg.text.trim(),
          address: addressMsg.text.trim()
        } 
      },
      { runValidators: true }
    ).exec();
    
    const options = isAdmin(chatId) ? adminMainMenu() : userMainMenu();
    await bot.sendMessage(chatId, '✅ Профиль сохранен!', options);
  } catch (error) {
    console.error('Profile setup error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка сохранения профиля или время ожидания истекло. Попробуйте снова: /start');
  }
};

// ========================
// 7. ОБРАБОТКА ЗАПИСИ НА ОТКАЧКУ (с календарём)
// ========================
bot.onText(/🚗 Записаться на откачку/, async (msg) => {
  const chatId = msg.chat.id;
  
  try {
    const user = await User.findOne({ telegramId: chatId }).exec();
    if (!user || !user.name || !user.phone || !user.address) {
      await bot.sendMessage(chatId, 'Пожалуйста, сначала заполните ваш профиль');
      return askForUserDetails(chatId);
    }

    const cars = await Car.find({ isActive: true }).exec();
    if (cars.length === 0) {
      return await bot.sendMessage(chatId, 'В настоящее время нет доступных автомобилей.');
    }
    
    const buttons = cars.map(car => ({
      text: `${car.brand} (${car.licensePlate})`,
      callback_data: `select_car_${car._id}`
    }));
    
    await bot.sendMessage(chatId, 'Выберите автомобиль:', {
      reply_markup: {
        inline_keyboard: buttons.map(btn => [btn])
      }
    });
  } catch (error) {
    console.error('Pump scheduling error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка при загрузке автомобилей');
  }
});

// ========================
// 8. ПОКАЗ КАЛЕНДАРЯ (год → месяц → день → время)
// ========================
const showYearSelection = async (chatId, carId) => {
  try {
    const now = new Date();
    const y = now.getFullYear();
    const years = [y, y + 1];
    const buttons = years.map(year => ({
      text: String(year),
      callback_data: `book:y:${carId}:${year}`
    }));

    await bot.sendMessage(chatId, '📅 Выберите год:', {
      reply_markup: { inline_keyboard: [buttons] }
    });
  } catch (error) {
    console.error('Year selection error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка при загрузке списка лет');
  }
};

const showMonthSelection = async (chatId, carId, year) => {
  try {
    const now = new Date();
    const currentYear = now.getFullYear();
    const currentMonthIndex = now.getMonth(); // 0-11

    const months = [];
    for (let m = 0; m < 12; m++) {
      if (year > currentYear || (year === currentYear && m >= currentMonthIndex)) {
        months.push({ idx: m + 1, label: MONTHS_SHORT_RU[m] });
      }
    }

    if (!months.length) {
      return bot.sendMessage(chatId, '🚫 Нет доступных месяцев в выбранном году');
    }

    const rows = [];
    for (let i = 0; i < months.length; i += 3) {
      const row = months.slice(i, i + 3).map(m => ({
        text: m.label,
        callback_data: `book:m:${carId}:${year}:${m.idx}`
      }));
      rows.push(row);
    }

    await bot.sendMessage(chatId, `📅 Год ${year}. Выберите месяц:`, {
      reply_markup: { inline_keyboard: rows }
    });
  } catch (error) {
    console.error('Month selection error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка при загрузке месяцев');
  }
};

const showDaySelection = async (chatId, carId, year, month) => {
  try {
    const car = await Car.findById(carId).exec();
    if (!car) {
      return await bot.sendMessage(chatId, '🚫 Автомобиль не найден');
    }

    const now = new Date();
    const firstOfMonth = new Date(year, month - 1, 1);
    const daysInMonth = new Date(year, month, 0).getDate();

    const allowedDays = [];
    for (let day = 1; day <= daysInMonth; day++) {
      const d = new Date(year, month - 1, day);
      if (year === now.getFullYear() && month - 1 === now.getMonth() && d < new Date(now.getFullYear(), now.getMonth(), now.getDate())) {
        continue;
      }
      if (car.schedule.days.includes(d.getDay())) {
        allowedDays.push(day);
      }
    }

    if (!allowedDays.length) {
      return bot.sendMessage(chatId, '🚫 В этом месяце нет доступных дней для записи');
    }

    const rows = [];
    const maxCols = 7;
    for (let i = 0; i < allowedDays.length; i += maxCols) {
      const row = allowedDays.slice(i, i + maxCols).map(day => ({
        text: String(day),
        callback_data: `book:d:${carId}:${year}:${month}:${day}`
      }));
      rows.push(row);
    }

    await bot.sendMessage(chatId, `📅 ${MONTHS_SHORT_RU[month - 1]} ${year}. Выберите день:`, {
      reply_markup: { inline_keyboard: rows }
    });
  } catch (error) {
    console.error('Day selection error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка при загрузке дней');
  }
};

const showTimeSlots = async (chatId, carId, year, month, day) => {
  try {
    const car = await Car.findById(carId).exec();
    if (!car) {
      return await bot.sendMessage(chatId, '🚫 Автомобиль не найден');
    }

    const selectedDate = new Date(year, month - 1, day);
    const availableHours = await getAvailableHours(carId, selectedDate, car.schedule.hours);

    const now = new Date();
    const filteredHours = availableHours.filter(hour => {
      if (isSameYMD(selectedDate, now)) {
        return hour > now.getHours();
      }
      return true;
    });

    if (!filteredHours.length) {
      return await bot.sendMessage(chatId, '🚫 Нет доступного времени для записи');
    }

    const rows = [];
    const cols = 3;
    for (let i = 0; i < filteredHours.length; i += cols) {
      const row = filteredHours.slice(i, i + cols).map(hour => ({
        text: formatHourSlotLabel(hour),
        callback_data: `book:t:${carId}:${year}:${month}:${day}:${hour}`
      }));
      rows.push(row);
    }

    await bot.sendMessage(chatId, '⏰ Выберите время:', {
      reply_markup: { inline_keyboard: rows }
    });
  } catch (error) {
    console.error('Time slots error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка при загрузке доступного времени');
  }
};

const createOrder = async (chatId, carId, datetime) => {
  try {
    const [user, car] = await Promise.all([
      User.findOne({ telegramId: chatId }).exec(),
      Car.findById(carId).exec()
    ]);

    if (!user || !car) {
      return await bot.sendMessage(chatId, '🚫 Ошибка создания заказа. Данные не найдены.');
    }

    const existingOrder = await Order.findOne({
      car: carId,
      datetime,
      status: { $in: ['new', 'confirmed'] }
    }).exec();

    if (existingOrder) {
      return await bot.sendMessage(chatId, '⚠️ Это время уже занято, выберите другое');
    }

    const order = new Order({
      user: user._id,
      car: car._id,
      datetime,
      status: 'new'
    });

    await order.save();

    await bot.sendMessage(chatId, 
      `✅ Заказ успешно создан!\n\n` +
      `📅 Дата: ${formatDate(datetime)}\n` +
      `🚗 Автомобиль: ${car.brand} (${car.licensePlate})\n\n` +
      `Ожидайте подтверждения от администратора.`
    );

    const adminIds = process.env.ADMIN_IDS.split(',').map(id => id.trim());
    await Promise.all(adminIds.map(async adminId => {
      try {
        await bot.sendMessage(adminId,
          `🆕 Новый заказ!\n\n` +
          `👤 Клиент: ${user.name || 'Не указано'}\n` +
          `📞 Телефон: ${user.phone || 'Не указан'}\n` +
          `🚗 Авто: ${car.brand} (${car.licensePlate})\n` +
          `📅 Дата: ${formatDate(datetime)}\n` +
          `🆔 ID: ${order._id}`
        );
      } catch (e) {
        console.error(`Не удалось уведомить администратора ${adminId}:`, e);
      }
    }));
  } catch (error) {
    console.error('Create order error:', error);
    await bot.sendMessage(chatId, '⚠️ Ошибка при создании заказа');
  }
};

// ========================
// 9. АДМИНИСТРАТИВНЫЕ ФУНКЦИИ (минимально, как в примере)
// ========================
let adminState = {};

const handleCarManagement = async (chatId) => {
  const keyboard = {
    inline_keyboard: [
      [{ text: 'Добавить автомобиль', callback_data: 'admin_add_car' }],
      [{ text: 'Список автомобилей', callback_data: 'admin_list_cars' }],
      [{ text: 'Изменить статус', callback_data: 'admin_toggle_car' }]
    ]
  };
  
  await bot.sendMessage(chatId, 'Выберите действие:', { reply_markup: keyboard });
};

const handleOrderManagement = async (chatId) => {
  const keyboard = {
    inline_keyboard: [
      [{ text: 'Новые заказы', callback_data: 'admin_new_orders' }],
      [{ text: 'Подтвержденные', callback_data: 'admin_confirmed_orders' }],
      [{ text: 'Все заказы', callback_data: 'admin_all_orders' }]
    ]
  };
  
  await bot.sendMessage(chatId, 'Выберите тип заказов:', { reply_markup: keyboard });
};

const handleUserManagement = async (chatId) => {
  const keyboard = {
    inline_keyboard: [
      [{ text: 'Список клиентов', callback_data: 'admin_list_users' }],
      [{ text: 'Поиск клиента', callback_data: 'admin_find_user' }]
    ]
  };
  
  await bot.sendMessage(chatId, 'Выберите действие:', { reply_markup: keyboard });
};

// ========================
// 10. ОБРАБОТКА СООБЩЕНИЙ И CALLBACK-ЗАПРОСОВ
// ========================
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = msg.text;

  if (!isAdmin(chatId)) return;

  if (text === '🚗 Управление автомобилями') {
    return handleCarManagement(chatId);
  }
  if (text === '📋 Управление заказами') {
    return handleOrderManagement(chatId);
  }
  if (text === '👥 Управление клиентами') {
    return handleUserManagement(chatId);
  }
  if (text === '📢 Рассылка') {
    adminState[chatId] = { action: 'broadcast' };
    return bot.sendMessage(chatId, 'Введите сообщение для рассылки:');
  }
  if (text === '📊 Статистика') {
    const [usersCount, activeCars, newOrdersCount] = await Promise.all([
      User.countDocuments().exec(),
      Car.countDocuments({ isActive: true }).exec(),
      Order.countDocuments({ status: 'new' }).exec()
    ]);
    
    return bot.sendMessage(chatId,
      `📊 Статистика системы:\n\n` +
      `👥 Всего пользователей: ${usersCount}\n` +
      `🚗 Активных автомобилей: ${activeCars}\n` +
      `🆕 Новых заказов: ${newOrdersCount}`
    );
  }
});

bot.on('callback_query', async (query) => {
  const chatId = query.message.chat.id;
  const data = query.data;
  
  try {
    // Выбор автомобиля → переход к выбору года
    if (data.startsWith('select_car_')) {
      const carId = data.split('_')[2];
      await showYearSelection(chatId, carId);
    }
    // Календарь: год → месяц → день → время
    else if (data.startsWith('book:')) {
      const parts = data.split(':');
      const kind = parts[1];
      if (kind === 'y') {
        const carId = parts[2];
        const year = parseInt(parts[3], 10);
        await showMonthSelection(chatId, carId, year);
      } else if (kind === 'm') {
        const carId = parts[2];
        const year = parseInt(parts[3], 10);
        const month = parseInt(parts[4], 10);
        await showDaySelection(chatId, carId, year, month);
      } else if (kind === 'd') {
        const carId = parts[2];
        const year = parseInt(parts[3], 10);
        const month = parseInt(parts[4], 10);
        const day = parseInt(parts[5], 10);
        await showTimeSlots(chatId, carId, year, month, day);
      } else if (kind === 't') {
        const carId = parts[2];
        const year = parseInt(parts[3], 10);
        const month = parseInt(parts[4], 10);
        const day = parseInt(parts[5], 10);
        const hour = parseInt(parts[6], 10);
        const datetime = new Date(year, month - 1, day, hour, 0, 0, 0);
        await createOrder(chatId, carId, datetime);
      }
      await bot.answerCallbackQuery(query.id);
      return;
    }

    await bot.answerCallbackQuery(query.id);
  } catch (error) {
    console.error('Callback processing error:', error);
    try { await bot.answerCallbackQuery(query.id); } catch (_) {}
    await bot.sendMessage(chatId, '⚠️ Произошла ошибка при обработке запроса');
  }
});

// ========================
// 11. ЗАПУСК СЕРВЕРА
// ========================
app.get('/', (req, res) => {
  res.status(200).json({ 
    status: 'Bot is running', 
    time: new Date().toISOString(),
    version: '1.1.0'
  });
});

app.listen(PORT, () => {
  console.log(`🚀 Сервер запущен на порту ${PORT}`);
});

// Обработка завершения работы
process.on('SIGINT', async () => {
  try { await bot.stopPolling(); } catch (_) {}
  await mongoose.disconnect();
  console.log('MongoDB disconnected on app termination');
  process.exit(0);
});