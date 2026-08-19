CREATE TABLE authors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors (id),
    title TEXT NOT NULL
);

CREATE INDEX books_by_author ON books (author_id);

CREATE VIEW books_with_authors AS
SELECT books.id AS book_id,
       books.title AS title,
       authors.name AS author
FROM books
JOIN authors ON authors.id = books.author_id;
