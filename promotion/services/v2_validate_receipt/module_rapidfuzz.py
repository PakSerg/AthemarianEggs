from rapidfuzz import fuzz


def valid_rapidfuzz(items, keywords):
    try:
        for item in items:
            item_name = item.get('name', '').lower()
            if not item_name:
                continue

            for keyword in keywords:
                wratio = fuzz.WRatio(keyword, item_name)

                partial = fuzz.partial_ratio(keyword, item_name)

                if wratio >= 65 and partial >= 55:
                    return {
                        'status': True,
                        'wratio': wratio,
                        'partial': partial,
                        'product': item_name
                    }

        return {
            'status': False
        }

    except Exception as e:
        return {
            'status': False
        }
