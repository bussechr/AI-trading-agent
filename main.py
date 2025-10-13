"""Main entry point for the AI Trading Agent."""

import argparse
import logging
import yaml
import sys
from pathlib import Path

from src.agent.trading_agent import TradingAgent


def setup_logging(level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('trading_agent.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )


def load_config(config_path: str) -> dict:
    """
    Load configuration from YAML file.
    
    Args:
        config_path: Path to config file
        
    Returns:
        Configuration dictionary
    """
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='AI Trading Agent')
    parser.add_argument(
        '--config',
        type=str,
        default='config/config.yaml',
        help='Path to configuration file'
    )
    parser.add_argument(
        '--mode',
        type=str,
        choices=['live', 'backtest', 'analyze'],
        default='live',
        help='Operating mode'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help='Trading cycle interval in seconds'
    )
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing config file: {e}")
        sys.exit(1)
    
    # Setup logging
    setup_logging(config.get('log_level', 'INFO'))
    logger = logging.getLogger(__name__)
    
    logger.info("=" * 60)
    logger.info("AI Trading Agent Starting")
    logger.info("=" * 60)
    logger.info(f"Mode: {args.mode}")
    logger.info(f"Config: {args.config}")
    logger.info(f"Symbols: {config.get('symbols', [])}")
    logger.info("=" * 60)
    
    # Initialize and run agent
    try:
        agent = TradingAgent(config)
        
        if args.mode == 'live':
            agent.run(interval=args.interval)
        elif args.mode == 'analyze':
            # Just run one cycle and show results
            if agent.start():
                agent.execute_trading_cycle()
                status = agent.get_portfolio_status()
                print("\n" + "=" * 60)
                print("Portfolio Status:")
                print("=" * 60)
                print(yaml.dump(status, default_flow_style=False))
                agent.stop()
        elif args.mode == 'backtest':
            logger.error("Backtest mode not yet implemented")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.info("\nShutdown requested... exiting")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
