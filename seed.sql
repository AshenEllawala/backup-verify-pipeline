CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    amount DECIMAL(10,2) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO users (name, email) VALUES
('Alice Fernando', 'alice@example.com'),
('Kasun Perera', 'kasun@example.com'),
('Nimali Silva', 'nimali@example.com');

INSERT INTO orders (user_id, amount, status) VALUES
(1, 1500.00, 'completed'),
(1, 850.50, 'pending'),
(2, 2200.00, 'completed'),
(3, 430.75, 'pending');