from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from product_decision_engine.domain.catalog import Catalog
from product_decision_engine.domain.models import (
    AvailabilityObservation,
    OfferAvailability,
    ProductLifecycleObservation,
    ProductLifecycleStatus,
    RetailerBasketAudit,
    UsageScenario,
    VerificationStatus,
)
from product_decision_engine.evaluation.retailer_baskets import (
    evaluate_retailer_basket,
)
from product_decision_engine.market import (
    FreshnessPolicy,
    audit_market_eligibility,
    availability_from_retailer_audits,
)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _rub(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def _percent(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0,0%"
    return f"{numerator * 100 / denominator:.1f}%".replace(".", ",")


def _device_check_availability(
    snapshot: dict[str, Any],
) -> tuple[AvailabilityObservation, ...]:
    state_map = {
        "current_offers": OfferAvailability.IN_STOCK,
        "no_current_offers": OfferAvailability.UNAVAILABLE,
        "not_verified": OfferAvailability.UNVERIFIED,
    }
    return tuple(
        AvailabilityObservation(
            id=f"{snapshot['id']}:{item['product_id']}",
            product_id=item["product_id"],
            source_provider_key=snapshot["provider"]["provider_key"],
            availability=state_map[item["status"]],
            observed_at=snapshot["observed_at"],
            verification_status=VerificationStatus(item["verification_status"]),
            source_url=item["source_url"],
        )
        for item in snapshot["device_checks"]
    )


def _lifecycle_observations(
    snapshot: dict[str, Any],
) -> tuple[ProductLifecycleObservation, ...]:
    return tuple(
        ProductLifecycleObservation(
            id=f"{snapshot['id']}:lifecycle:{item['product_id']}",
            product_id=item["product_id"],
            source_provider_key=snapshot["provider"]["provider_key"],
            lifecycle_status=ProductLifecycleStatus(item["lifecycle_status"]),
            observed_at=snapshot["observed_at"],
            verification_status=VerificationStatus(item["verification_status"]),
            source_url=item["source_url"],
            notes=item["notes"],
        )
        for item in snapshot["lifecycle_checks"]
    )


def build_phase1_market_report(
    catalog: Catalog,
    scenarios: tuple[UsageScenario, ...],
    cohort: list[dict[str, Any]],
    snapshot: dict[str, Any],
    golden_audits: tuple[RetailerBasketAudit, ...],
    price_ru_audits: tuple[RetailerBasketAudit, ...],
) -> str:
    cohort_ids = tuple(item["product_id"] for item in cohort)
    if len(cohort_ids) < 12:
        raise ValueError("Phase 1 cohort must contain at least 12 products")
    if len(set(cohort_ids)) != len(cohort_ids):
        raise ValueError("Phase 1 cohort product ids must be unique")
    for product_id in cohort_ids:
        catalog.product(product_id)

    as_of = date.fromisoformat(snapshot["observed_at"])
    policy = FreshnessPolicy()
    all_audits = (*golden_audits, *price_ru_audits)
    availability = (
        *availability_from_retailer_audits(all_audits),
        *_device_check_availability(snapshot),
    )
    lifecycle = _lifecycle_observations(snapshot)
    market_audits = tuple(
        audit_market_eligibility(
            catalog,
            catalog.product(product_id),
            availability,
            lifecycle,
            as_of=as_of,
            policy=policy,
        )
        for product_id in cohort_ids
    )

    device_checks = snapshot["device_checks"]
    current_offer_checks = tuple(
        item
        for item in device_checks
        if item["verification_status"] == "verified"
        and item["status"] == "current_offers"
    )
    no_offer_checks = tuple(
        item
        for item in device_checks
        if item["verification_status"] == "verified"
        and item["status"] == "no_current_offers"
    )
    unverified_checks = tuple(
        item for item in device_checks if item["verification_status"] != "verified"
    )

    fresh_complete_product_ids = {
        offer.product_id
        for audit in all_audits
        if audit.verification_status == VerificationStatus.VERIFIED
        and 0 <= (as_of - date.fromisoformat(audit.observed_at)).days <= 7
        for offer in audit.offers
        if offer.complete
    } & set(cohort_ids)

    market_eligible = tuple(item for item in market_audits if item.eligible)
    blockers = Counter(
        reason.split(":", 1)[0]
        for item in market_audits
        for reason in item.blocking_reasons
    )

    scenario = next(
        item for item in scenarios if item.id == price_ru_audits[0].scenario_id
    )
    current_result = evaluate_retailer_basket(
        catalog, scenario, price_ru_audits[0]
    )
    prior_regard = next(
        item
        for item in golden_audits
        if item.id == "home-light-pair-regard-20260820"
    )
    prior_result = evaluate_retailer_basket(catalog, scenario, prior_regard)

    lines = [
        "# Фаза 1 — проверка российского рынка и свежести данных",
        "",
        f"Срез: **{snapshot['observed_at']}**, рынок: **Россия / Москва / RUB**.",
        "",
        "## Промежуточное решение",
        "",
        "Статус: **`CONTINUE PHASE 1 / M2–M3`**. Price.ru полезен как агрегатор "
        "цен и наличия, но публичные страницы нельзя превращать в production-scraper. "
        "Для production проверяется официальный партнёрский XML-фид. Текущий срез "
        "ещё не достигает порога полной корзины и не даёт `GO` к web MVP.",
        "",
        "## Когорта и покрытие первого среза",
        "",
        f"- Моделей в когорте: **{len(cohort_ids)}**.",
        f"- Карточек с подтверждёнными текущими предложениями Price.ru: "
        f"**{len(current_offer_checks)} / {len(cohort_ids)} "
        f"({_percent(len(current_offer_checks), len(cohort_ids))})**.",
        f"- Карточек без текущих предложений: **{len(no_offer_checks)} / {len(cohort_ids)}**.",
        f"- Точных карточек, не подтверждённых этой ручной проверкой: "
        f"**{len(unverified_checks)} / {len(cohort_ids)}**.",
        f"- Полных свежих корзин устройства и OEM-расходников: "
        f"**{len(fresh_complete_product_ids)} / {len(cohort_ids)} "
        f"({_percent(len(fresh_complete_product_ids), len(cohort_ids))})**.",
        f"- Моделей, прошедших формальный freshness/lifecycle gate: "
        f"**{len(market_eligible)} / {len(cohort_ids)} "
        f"({_percent(len(market_eligible), len(cohort_ids))})**.",
        "",
        "Ключевое различие: наличие карточки или даже устройства в продаже ещё не "
        "означает наличие полной OEM-корзины для воспроизводимого TCO.",
        "",
        "## Результат freshness/lifecycle gate",
        "",
        f"Политика среза: цена не старше **{policy.price_max_age_days} дней**, "
        f"наличие не старше **{policy.availability_max_age_days} дней**.",
        "",
        "| Модель | Допуск | Причины блокировки | Предупреждения |",
        "|---|---:|---|---|",
    ]
    for item in market_audits:
        product = catalog.product(item.product_id)
        lines.append(
            f"| {product.manufacturer} {product.model} | "
            f"{'да' if item.eligible else 'нет'} | "
            f"{'; '.join(item.blocking_reasons) or '—'} | "
            f"{'; '.join(item.warnings) or '—'} |"
        )

    lines.extend(
        [
            "",
            "Сводка причин блокировки: "
            + (", ".join(f"`{key}` — {value}" for key, value in sorted(blockers.items())) or "нет")
            + ".",
            "",
            "`lifecycle_unknown` является предупреждением, а не выдуманным выводом о "
            "снятии с производства. Свежая цена и наличие могут временно допустить модель; "
            "подтверждённый `discontinued` всегда блокирует её.",
            "",
            "## Повторная цена прямой картриджной пары",
            "",
            f"Сценарий: **{scenario.name}**. Сравнивается одна корзина продавца "
            "Regard, найденная через Price.ru; совместимые картриджи исключены по exact "
            "OEM part number.",
            "",
            "| Показатель | Предыдущая проверка Regard | Price.ru → Regard |",
            "|---|---:|---:|",
            f"| Цена Canon PIXMA TS3640 | "
            f"{_rub(prior_result.product_result('canon-pixma-ts3640').full_tco.purchase_cost_rub)} | "
            f"{_rub(current_result.product_result('canon-pixma-ts3640').full_tco.purchase_cost_rub)} |",
            f"| TCO Canon PIXMA TS3640 | "
            f"{_rub(prior_result.product_result('canon-pixma-ts3640').full_tco.total_cost_rub)} | "
            f"{_rub(current_result.product_result('canon-pixma-ts3640').full_tco.total_cost_rub)} |",
            f"| Цена HP DeskJet 2875 | "
            f"{_rub(prior_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.purchase_cost_rub)} | "
            f"{_rub(current_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.purchase_cost_rub)} |",
            f"| TCO HP DeskJet 2875 | "
            f"{_rub(prior_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.total_cost_rub)} | "
            f"{_rub(current_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.total_cost_rub)} |",
            "",
            f"Предыдущий baseline по цене покупки выбирал **{prior_result.purchase_price_winner.product.manufacturer} "
            f"{prior_result.purchase_price_winner.product.model}**, а Decision Engine — "
            f"**{prior_result.decision_engine_winner.product.manufacturer} "
            f"{prior_result.decision_engine_winner.product.model}**. В новом срезе и baseline, "
            f"и TCO выбирают **{current_result.decision_engine_winner.product.manufacturer} "
            f"{current_result.decision_engine_winner.product.model}**, потому что HP теперь "
            "дешевле уже при покупке. Это нормальная смена результата из-за цены, а не "
            "ошибка движка.",
            "",
            "## Проверка источника",
            "",
            f"- Условия публичных страниц: {snapshot['provider']['terms_url']}",
            f"- Официальный путь к XML-фиду: {snapshot['provider']['partner_program_url']}",
            "- Публичная автоматическая выгрузка без разрешения: **restricted**.",
            "- Роль Price.ru: цена и наличие; technical facts и yields остаются за "
            "официальными источниками производителей.",
            "- Минимум карточки расходника нельзя брать автоматически: в одной карточке "
            "могут смешиваться OEM и совместимые предложения.",
            "",
            "## Что проверяется дальше",
            "",
            "1. Получение условий партнёрского XML-фида и полей, достаточных для exact SKU, "
            "retailer, price и availability.",
            "2. Добор полных OEM-корзин минимум до 80% когорты или честное сужение когорты.",
            "3. Ещё два среза в разные дни так, чтобы общий интервал составил не менее 14 дней.",
            "4. Учёт фактического ручного времени и специальных исключений начиная со "
            "следующего среза; для текущего ретроспективного среза время не выдумывается.",
            "",
            "## Риски",
            "",
            "- На первом срезе полная свежая корзина существенно ниже целевых 80%; это "
            "главный текущий риск, но ещё не финальный провал M3.",
            "- Исчезновение предложения нельзя путать со снятием модели с производства.",
            "- Без exact MPN/part number агрегатор может вернуть совместимый расходник как "
            "самый дешёвый и занизить TCO.",
            "- Если официальный feed недоступен на приемлемых условиях, вариант со scraping "
            "публичных страниц не рассматривается как устойчивое production-решение.",
            "",
        ]
    )
    return "\n".join(lines)
