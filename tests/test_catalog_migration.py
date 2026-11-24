import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from deploy.import_catalog import export_catalog, load_catalog


class CatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'source.db'
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        filename = 'a' * 32 + '.jpg'
        (self.uploads / filename).write_bytes(b'image')
        with closing(sqlite3.connect(self.database)) as source, source:
            source.execute('CREATE TABLE categories (id TEXT, name TEXT)')
            source.execute('''CREATE TABLE products (id TEXT, category_id TEXT, name TEXT,
                description TEXT, price_pence INTEGER, stock INTEGER, image_url TEXT, active INTEGER)''')
            source.execute('INSERT INTO categories VALUES (?,?)', ('category', 'Chairs'))
            source.execute('''INSERT INTO products VALUES (?,?,?,?,?,?,?,?)''',
                           ('product', 'category', 'Chair', 'Chair description', 350, 8,
                            f'https://old.example/uploads/{filename}', 1))

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_catalogue_and_normalizes_uploaded_image_url(self):
        categories, products, images = load_catalog(self.database, self.uploads)
        self.assertEqual(categories, [{'id': 'category', 'name': 'Chairs'}])
        self.assertEqual(products[0]['image_url'], f'/uploads/{"a" * 32}.jpg')
        self.assertEqual([(image[0], image[2]) for image in images],
                         [(f'{"a" * 32}.jpg', 'image/jpeg')])

    def test_missing_source_image_fails_before_migration(self):
        (self.uploads / f'{"a" * 32}.jpg').unlink()
        with self.assertRaises(FileNotFoundError):
            load_catalog(self.database, self.uploads)

    def test_export_contains_only_catalogue_and_referenced_images(self):
        with closing(sqlite3.connect(self.database)) as source, source:
            source.execute('CREATE TABLE bookings (id TEXT)')
            source.execute("INSERT INTO bookings VALUES ('private-booking')")
        categories, products, images = load_catalog(self.database, self.uploads)
        destination = self.root / 'export'
        export_catalog(destination, categories, products, images)
        with closing(sqlite3.connect(destination / 'catalog.db')) as exported:
            tables = {row[0] for row in exported.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'categories', 'products'})
        self.assertEqual(load_catalog(destination / 'catalog.db', destination / 'uploads'),
                         (categories, products,
                          [(images[0][0], destination / 'uploads' / images[0][0], images[0][2])]))
        with self.assertRaises(FileExistsError):
            export_catalog(destination, categories, products, images)


if __name__ == '__main__':
    unittest.main()
