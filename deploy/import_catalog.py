import argparse
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_catalog(database_path, uploads_path):
    with closing(sqlite3.connect(database_path)) as source, source:
        source.row_factory = sqlite3.Row
        source.execute('BEGIN')
        categories = [dict(row) for row in source.execute('SELECT id,name FROM categories ORDER BY id')]
        products = [dict(row) for row in source.execute('''SELECT id,category_id,name,description,
            price_pence,stock,image_url,active FROM products ORDER BY id''')]
    images = []
    for product in products:
        image_url = product['image_url']
        path = urlparse(image_url).path
        if path.startswith('/uploads/'):
            filename = Path(path).name
            if not re.fullmatch(r'[0-9a-f]{32}\.(?:jpg|png|webp)', filename):
                raise ValueError(f'Unsupported product image path in the source catalogue: {filename}')
            image_path = uploads_path / filename
            if not image_path.is_file():
                raise FileNotFoundError(f'Product image is missing from the source upload folder: {filename}')
            extension = image_path.suffix.lower()
            content_type = {'.jpg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}.get(extension)
            if not content_type:
                raise ValueError(f'Unsupported product image extension: {extension}')
            images.append((filename, image_path, content_type))
            product['image_url'] = f'/uploads/{filename}'
    return categories, products, images


def export_catalog(destination, categories, products, images):
    destination.mkdir(parents=True, exist_ok=False)
    uploads = destination / 'uploads'
    uploads.mkdir()
    with closing(sqlite3.connect(destination / 'catalog.db')) as target, target:
        target.execute('CREATE TABLE categories (id TEXT, name TEXT)')
        target.execute('''CREATE TABLE products (id TEXT, category_id TEXT, name TEXT,
            description TEXT, price_pence INTEGER, stock INTEGER, image_url TEXT, active INTEGER)''')
        target.executemany('INSERT INTO categories VALUES (:id,:name)', categories)
        target.executemany('''INSERT INTO products VALUES
            (:id,:category_id,:name,:description,:price_pence,:stock,:image_url,:active)''', products)
    for filename, image_path, _ in images:
        shutil.copyfile(image_path, uploads / filename)


def apply_catalog(categories, products, images):
    import app

    if not app.DATABASE_URL or not app.AZURE_STORAGE_ACCOUNT_URL:
        raise RuntimeError('DATABASE_URL and AZURE_STORAGE_ACCOUNT_URL are required for --apply')
    container = app.get_image_container()
    if container is None:
        raise RuntimeError('Azure Blob Storage is not configured')
    with app.db() as target:
        category_count = target.execute('SELECT COUNT(*) AS count FROM categories').fetchone()['count']
        product_count = target.execute('SELECT COUNT(*) AS count FROM products').fetchone()['count']
        if category_count or product_count:
            raise RuntimeError('Target catalogue is not empty; refusing to merge or overwrite products')
        for filename, image_path, content_type in images:
            with image_path.open('rb') as image:
                container.upload_blob(
                    name=filename,
                    data=image,
                    overwrite=True,
                    content_settings=app.ContentSettings(content_type=content_type),
                )
        for category in categories:
            target.execute('INSERT INTO categories(id,name) VALUES (?,?)',
                           (category['id'], category['name']))
        for product in products:
            target.execute('''INSERT INTO products
                (id,category_id,name,description,price_pence,stock,image_url,active)
                VALUES (?,?,?,?,?,?,?,?)''',
                (product['id'], product['category_id'], product['name'], product['description'],
                 product['price_pence'], product['stock'], product['image_url'], product['active']))


def main():
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(
        description='Import catalogue, stock and product images only; booking data is never read.')
    parser.add_argument('--source-db', type=Path, default=ROOT / 'bookings.db')
    parser.add_argument('--source-uploads', type=Path, default=ROOT / 'uploads')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true', help='Write to Azure PostgreSQL and Blob Storage')
    action.add_argument('--export-to', type=Path, help='Create a new catalogue-only export directory')
    args = parser.parse_args()
    if not args.source_db.is_file():
        raise FileNotFoundError(f'Source database not found: {args.source_db}')
    categories, products, images = load_catalog(args.source_db, args.source_uploads)
    if args.export_to:
        export_catalog(args.export_to, categories, products, images)
        print(f'Exported {len(categories)} categories, {len(products)} products, and {len(images)} images.')
    elif args.apply:
        apply_catalog(categories, products, images)
        print(f'Imported {len(categories)} categories, {len(products)} products, and {len(images)} images.')
    else:
        print(f'Dry run: {len(categories)} categories, {len(products)} products, and {len(images)} images.')
        print('No data was written. Review the counts, then rerun with --apply to import.')


if __name__ == '__main__':
    main()
